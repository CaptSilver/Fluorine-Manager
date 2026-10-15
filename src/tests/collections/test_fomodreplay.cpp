#include "collections/fomodreplay.h"

#include <QJsonArray>
#include <QJsonObject>

#include <gtest/gtest.h>

#include <optional>
#include <ostream>
#include <vector>

// Readable QString output in gtest failure messages. Internal linkage so other
// test files can define their own without a duplicate symbol.
static void PrintTo(const QString &text, std::ostream *os) {
  *os << '"' << qPrintable(text) << '"';
}

namespace {
struct Pick {
  QString plugin;
  int idx = -1; // negative: the manifest recorded no idx
};

struct GroupPicks {
  QString group;
  std::vector<Pick> picks;
};

struct StepPicks {
  QString step;
  std::vector<GroupPicks> groups;
};

// The shape the manifest stores under "choices".
QJsonObject choiceFor(const std::vector<StepPicks> &steps) {
  QJsonArray options;
  for (const StepPicks &step : steps) {
    QJsonArray groups;
    for (const GroupPicks &group : step.groups) {
      QJsonArray picks;
      for (const Pick &pick : group.picks) {
        QJsonObject choice{{"name", pick.plugin}};
        if (pick.idx >= 0) {
          choice.insert("idx", pick.idx);
        }
        picks.append(choice);
      }
      groups.append(QJsonObject{{"name", group.group}, {"choices", picks}});
    }
    options.append(QJsonObject{{"name", step.step}, {"groups", groups}});
  }
  return QJsonObject{{"type", "fomod"}, {"options", options}};
}

FomodModel parseModel(const QString &xml) {
  QString error;
  const std::optional<FomodModel> model =
      FomodModel::parse(xml.toUtf8(), error);
  EXPECT_TRUE(model.has_value()) << qPrintable(error);
  return model.value_or(FomodModel{});
}

struct Outcome {
  FomodSelection selection;
  QStringList warnings;
  QString error;
};

Outcome replay(const QString &xml, const QJsonObject &choices) {
  Outcome outcome;
  const FomodModel model = parseModel(xml);
  outcome.selection =
      FomodReplay::select(model, choices, outcome.warnings, outcome.error);
  return outcome;
}

QStringList sourcesOf(const FomodSelection &selection) {
  QStringList sources;
  for (const FileRule &rule : selection.rules) {
    sources << rule.source;
  }
  return sources;
}

// A choice that picks plugin "P" of group "G" in step "S".
QJsonObject pickP() { return choiceFor({{"S", {{"G", {{"P", 0}}}}}}); }

QString config(const QString &body) {
  return QStringLiteral("<config>") + body + QStringLiteral("</config>");
}

// Step "S", group "G", plugin "P" with the given <file>/<folder> elements and
// flags.
QString oneStep(const QString &filesXml, const QString &flagsXml = QString()) {
  return QStringLiteral(
             "<installSteps><installStep name=\"S\"><optionalFileGroups>"
             "<group name=\"G\"><plugins><plugin "
             "name=\"P\"><files>%1</files>%2</plugin>"
             "</plugins></group></optionalFileGroups></installStep></"
             "installSteps>")
      .arg(filesXml, flagsXml);
}

QString flagged(const QString &flagElements) {
  return oneStep(QString(),
                 "<conditionFlags>" + flagElements + "</conditionFlags>");
}

const QString DefaultPatternFile = QStringLiteral(
    R"(<file source="a.archive" destination="archive\pc\mod\p.archive"/>)");

QString pattern(const QString &dependencies,
                const QString &filesXml = DefaultPatternFile) {
  return QStringLiteral(
             "<conditionalFileInstalls><patterns><pattern>%1<files>%2</files>"
             "</pattern></patterns></conditionalFileInstalls>")
      .arg(dependencies, filesXml);
}

QString carXml() {
  return QString::fromUtf8(R"xml(<config>
 <requiredInstallFiles><folder source="core" destination="archive\pc\mod"/></requiredInstallFiles>
 <installSteps>
  <installStep name="Parts">
   <optionalFileGroups>
    <group name="Spoiler" type="SelectExactlyOne"><plugins>
      <plugin name="none"><files/></plugin>
      <plugin name="sp01"><files><file source="opt\sp01.archive" destination="archive\pc\mod\sp01.archive"/></files>
        <conditionFlags><flag name="spoiler">on</flag></conditionFlags></plugin>
      <plugin name="sp02"><files><file source="opt\sp02.archive" destination="archive\pc\mod\sp02.archive"/></files></plugin>
    </plugins></group>
   </optionalFileGroups>
  </installStep>
 </installSteps>
 <conditionalFileInstalls><patterns>
  <pattern><dependencies operator="And"><flagDependency flag="spoiler" value="on"/></dependencies>
    <files><file source="opt\extra.xl" destination="archive\pc\mod\extra.xl"/></files></pattern>
 </patterns></conditionalFileInstalls>
</config>)xml");
}

FomodCondition flagCondition(const QString &name, const QString &value) {
  FomodCondition condition;
  condition.kind = FomodCondition::Kind::Flag;
  condition.name = name;
  condition.value = value;
  return condition;
}

FomodCondition leafCondition(FomodCondition::Kind kind) {
  FomodCondition condition;
  condition.kind = kind;
  return condition;
}

FomodCondition combine(FomodCondition::Kind kind,
                       std::vector<FomodCondition> children) {
  FomodCondition condition;
  condition.kind = kind;
  condition.children = std::move(children);
  return condition;
}
} // namespace

// --- selection ---------------------------------------------------------------

TEST(FomodReplay, SelectsRecordedPluginAndMatchingPattern) {
  const Outcome outcome =
      replay(carXml(), choiceFor({{"Parts", {{"Spoiler", {{"sp01", 1}}}}}}));
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  EXPECT_EQ(outcome.selection.pluginNames, QStringList{"sp01"});
  EXPECT_EQ(sourcesOf(outcome.selection),
            (QStringList{"core", "opt/sp01.archive", "opt/extra.xl"}));
  ASSERT_EQ(outcome.selection.rules.size(), 3u);
  EXPECT_TRUE(outcome.selection.rules[0].isFolder);
  EXPECT_EQ(outcome.selection.rules[0].destination, QString("archive/pc/mod"));
  EXPECT_FALSE(outcome.selection.rules[1].isFolder);
  EXPECT_EQ(outcome.selection.rules[1].destination,
            QString("archive/pc/mod/sp01.archive"));
}

TEST(FomodReplay, SequenceCountsUpAcrossRequiredPluginAndPatternRules) {
  const Outcome outcome =
      replay(carXml(), choiceFor({{"Parts", {{"Spoiler", {{"sp01", 1}}}}}}));
  ASSERT_EQ(outcome.selection.rules.size(), 3u);
  EXPECT_EQ(outcome.selection.rules[0].sequence, 0);
  EXPECT_EQ(outcome.selection.rules[1].sequence, 1);
  EXPECT_EQ(outcome.selection.rules[2].sequence, 2);
}

TEST(FomodReplay, PatternNotAppliedWhenFlagNotSet) {
  const Outcome outcome =
      replay(carXml(), choiceFor({{"Parts", {{"Spoiler", {{"sp02", 2}}}}}}));
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  EXPECT_EQ(sourcesOf(outcome.selection),
            (QStringList{"core", "opt/sp02.archive"}));
}

TEST(FomodReplay, EmptySelectionInstallsOnlyRequired) {
  const Outcome outcome =
      replay(carXml(), choiceFor({{"Parts", {{"Spoiler", {}}}}}));
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  EXPECT_TRUE(outcome.selection.pluginNames.isEmpty());
  EXPECT_EQ(sourcesOf(outcome.selection), QStringList{"core"});
}

TEST(FomodReplay, EmptyOptionSkipped) {
  // An option with no picks is not looked up, so a step the installer does not
  // have is not an error.
  const Outcome outcome =
      replay(carXml(), choiceFor({{"NoSuchStep", {{"NoSuchGroup", {}}}}}));
  EXPECT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  EXPECT_EQ(sourcesOf(outcome.selection), QStringList{"core"});

  const Outcome noGroups = replay(
      carXml(),
      QJsonObject{{"options", QJsonArray{QJsonObject{{"name", "Parts"}}}}});
  EXPECT_TRUE(noGroups.error.isEmpty()) << qPrintable(noGroups.error);
}

TEST(FomodReplay, UnmatchedPluginNameIsError) {
  const Outcome outcome =
      replay(carXml(), choiceFor({{"Parts", {{"Spoiler", {{"sp99", 1}}}}}}));
  EXPECT_NE(outcome.error.indexOf("sp99"), -1) << qPrintable(outcome.error);
  EXPECT_TRUE(outcome.selection.rules.empty());
  EXPECT_TRUE(outcome.selection.pluginNames.isEmpty());
}

TEST(FomodReplay, UnmatchedStepOrGroupIsError) {
  const Outcome badStep =
      replay(carXml(), choiceFor({{"Nope", {{"Spoiler", {{"sp01", 1}}}}}}));
  EXPECT_NE(badStep.error.indexOf("Nope"), -1) << qPrintable(badStep.error);
  EXPECT_TRUE(badStep.selection.rules.empty());

  const Outcome badGroup =
      replay(carXml(), choiceFor({{"Parts", {{"Nope", {{"sp01", 1}}}}}}));
  EXPECT_NE(badGroup.error.indexOf("Nope"), -1) << qPrintable(badGroup.error);
  EXPECT_TRUE(badGroup.selection.rules.empty());
}

TEST(FomodReplay, FoldedNameLookupForStepAndGroup) {
  // Case and spacing differ from the XML at every level, plugin name included.
  const Outcome outcome =
      replay(carXml(), choiceFor({{"parts", {{"  SPOILER ", {{"SP01", 1}}}}}}));
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  EXPECT_EQ(outcome.selection.pluginNames, QStringList{"sp01"});
}

TEST(FomodReplay, AmbiguousFoldedMatchIsError) {
  const QString twoSteps = config(
      "<installSteps>"
      "<installStep name=\"Style\"><optionalFileGroups><group "
      "name=\"G\"><plugins>"
      "<plugin "
      "name=\"P\"><files/></plugin></plugins></group></optionalFileGroups>"
      "</installStep>"
      "<installStep name=\"STYLE\"><optionalFileGroups><group "
      "name=\"G\"><plugins>"
      "<plugin "
      "name=\"P\"><files/></plugin></plugins></group></optionalFileGroups>"
      "</installStep></installSteps>");
  const Outcome step =
      replay(twoSteps, choiceFor({{"style", {{"G", {{"P", 0}}}}}}));
  EXPECT_NE(step.error.indexOf("ambiguous"), -1) << qPrintable(step.error);

  const QString twoGroups =
      config("<installSteps><installStep name=\"S\"><optionalFileGroups>"
             "<group name=\"Opt\"><plugins><plugin "
             "name=\"P\"><files/></plugin></plugins></group>"
             "<group name=\"OPT\"><plugins><plugin "
             "name=\"P\"><files/></plugin></plugins></group>"
             "</optionalFileGroups></installStep></installSteps>");
  const Outcome group =
      replay(twoGroups, choiceFor({{"S", {{"opt", {{"P", 0}}}}}}));
  EXPECT_NE(group.error.indexOf("ambiguous"), -1) << qPrintable(group.error);
}

TEST(FomodReplay, ExactNameBeatsFoldedMatch) {
  // "Opt" is an exact match, so "OPT" is not a competing candidate.
  const QString twoGroups =
      config("<installSteps><installStep name=\"S\"><optionalFileGroups>"
             "<group name=\"Opt\"><plugins><plugin name=\"P\"><files>"
             "<file source=\"a.archive\" "
             "destination=\"a.archive\"/></files></plugin></plugins></group>"
             "<group name=\"OPT\"><plugins><plugin name=\"P\"><files>"
             "<file source=\"b.archive\" "
             "destination=\"b.archive\"/></files></plugin></plugins></group>"
             "</optionalFileGroups></installStep></installSteps>");
  const Outcome outcome =
      replay(twoGroups, choiceFor({{"S", {{"Opt", {{"P", 0}}}}}}));
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  EXPECT_EQ(sourcesOf(outcome.selection), QStringList{"a.archive"});
}

TEST(FomodReplay, DuplicatePluginNamesDisambiguatedByIdx) {
  const QString xml = config(
      "<installSteps><installStep name=\"S\"><optionalFileGroups><group "
      "name=\"G\"><plugins>"
      "<plugin name=\"Dup\"><files><file source=\"a.archive\" "
      "destination=\"a.archive\"/>"
      "</files></plugin>"
      "<plugin name=\"Dup\"><files><file source=\"b.archive\" "
      "destination=\"b.archive\"/>"
      "</files></plugin>"
      "</plugins></group></optionalFileGroups></installStep></installSteps>");

  const Outcome second = replay(xml, choiceFor({{"S", {{"G", {{"Dup", 1}}}}}}));
  ASSERT_TRUE(second.error.isEmpty()) << qPrintable(second.error);
  EXPECT_EQ(sourcesOf(second.selection), QStringList{"b.archive"});
  EXPECT_TRUE(second.warnings.isEmpty());

  const Outcome first = replay(xml, choiceFor({{"S", {{"G", {{"Dup", 0}}}}}}));
  ASSERT_TRUE(first.error.isEmpty()) << qPrintable(first.error);
  EXPECT_EQ(sourcesOf(first.selection), QStringList{"a.archive"});

  const Outcome noIdx = replay(xml, choiceFor({{"S", {{"G", {{"Dup", -1}}}}}}));
  EXPECT_NE(noIdx.error.indexOf("ambiguous"), -1) << qPrintable(noIdx.error);

  const Outcome outOfRange =
      replay(xml, choiceFor({{"S", {{"G", {{"Dup", 5}}}}}}));
  EXPECT_NE(outOfRange.error.indexOf("ambiguous"), -1)
      << qPrintable(outOfRange.error);
}

TEST(FomodReplay, IdxMismatchWarnsButSelectsByName) {
  const Outcome outcome =
      replay(carXml(), choiceFor({{"Parts", {{"Spoiler", {{"sp01", 2}}}}}}));
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  EXPECT_EQ(outcome.selection.pluginNames, QStringList{"sp01"});
  ASSERT_EQ(outcome.warnings.size(), 1);
  EXPECT_NE(outcome.warnings[0].indexOf("idx"), -1)
      << qPrintable(outcome.warnings[0]);
}

TEST(FomodReplay, MissingIdxOnUniqueNameIsNotAWarning) {
  const Outcome outcome =
      replay(carXml(), choiceFor({{"Parts", {{"Spoiler", {{"sp01", -1}}}}}}));
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  EXPECT_TRUE(outcome.warnings.isEmpty());
}

// --- flags and patterns
// --------------------------------------------------------

TEST(FomodReplay, FlagsAcrossStepsLastWriteWins) {
  const QString xml = config(
      "<installSteps>"
      "<installStep name=\"S1\"><optionalFileGroups><group name=\"G\"><plugins>"
      "<plugin name=\"P1\"><files/><conditionFlags><flag "
      "name=\"x\">1</flag></conditionFlags>"
      "</plugin></plugins></group></optionalFileGroups></installStep>"
      "<installStep name=\"S2\"><optionalFileGroups><group name=\"G\"><plugins>"
      "<plugin name=\"P2\"><files/><conditionFlags><flag "
      "name=\"x\">2</flag></conditionFlags>"
      "</plugin></plugins></group></optionalFileGroups></installStep>"
      "</installSteps>" +
      pattern(
          R"(<dependencies><flagDependency flag="x" value="2"/></dependencies>)"));

  // The choices list the later step first. Flags still follow document order,
  // so the second step's write is the last one and the pattern applies.
  const Outcome outcome = replay(
      xml,
      choiceFor({{"S2", {{"G", {{"P2", 0}}}}}, {"S1", {{"G", {{"P1", 0}}}}}}));
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  EXPECT_EQ(outcome.selection.pluginNames, (QStringList{"P1", "P2"}));
  EXPECT_EQ(sourcesOf(outcome.selection), QStringList{"a.archive"});
}

TEST(FomodReplay, FlagLastWriteWinsWithinOnePlugin) {
  const QString xml = config(
      flagged(R"(<flag name="x">1</flag><flag name="x">2</flag>)") +
      pattern(
          R"(<dependencies><flagDependency flag="x" value="2"/></dependencies>)"));
  const Outcome outcome = replay(xml, pickP());
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  EXPECT_EQ(sourcesOf(outcome.selection), QStringList{"a.archive"});
}

TEST(FomodReplay, FlagValuesAndNamesMatchExactly) {
  const std::map<QString, QString> flags{{"f", "On"}, {"Foo", "1"}};
  EXPECT_EQ(FomodReplay::evaluate(flagCondition("f", "on"), flags),
            FomodTriState::False);
  EXPECT_EQ(FomodReplay::evaluate(flagCondition("foo", "1"), flags),
            FomodTriState::False);
  EXPECT_EQ(FomodReplay::evaluate(flagCondition("f", "On"), flags),
            FomodTriState::True);
  // A flag nobody set reads as the empty string.
  EXPECT_EQ(FomodReplay::evaluate(flagCondition("unset", ""), flags),
            FomodTriState::True);
  EXPECT_EQ(FomodReplay::evaluate(flagCondition("unset", "1"), flags),
            FomodTriState::False);

  // The same through a whole replay: "On" set, "on" required.
  const QString xml = config(
      flagged(R"(<flag name="f">On</flag>)") +
      pattern(
          R"(<dependencies><flagDependency flag="f" value="on"/></dependencies>)"));
  const Outcome outcome = replay(xml, pickP());
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  EXPECT_TRUE(outcome.selection.rules.empty());
}

TEST(FomodReplay, OrAndNestedDependencies) {
  const QString xml = config(
      flagged(R"(<flag name="x">1</flag>)") +
      pattern(
          R"(<dependencies operator="Or"><flagDependency flag="y" value="1"/>)"
          R"(<dependencies operator="And"><flagDependency flag="x" value="1"/>)"
          R"(</dependencies></dependencies>)"));
  const Outcome outcome = replay(xml, pickP());
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  ASSERT_EQ(outcome.selection.rules.size(), 1u);
  EXPECT_EQ(outcome.selection.rules[0].destination,
            QString("archive/pc/mod/p.archive"));
}

TEST(FomodReplay, AndWithUnmetFlagSkipped) {
  const QString xml = config(
      flagged(R"(<flag name="x">1</flag>)") +
      pattern(
          R"(<dependencies operator="And"><flagDependency flag="x" value="1"/>)"
          R"(<flagDependency flag="y" value="1"/></dependencies>)"));
  const Outcome outcome = replay(xml, pickP());
  EXPECT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  EXPECT_TRUE(outcome.selection.rules.empty());
}

TEST(FomodReplay, PatternWithoutDependenciesAlwaysApplies) {
  const QString xml =
      config(oneStep(QString()) +
             QStringLiteral(
                 "<conditionalFileInstalls><patterns><pattern><files>%1</files>"
                 "</pattern></patterns></conditionalFileInstalls>")
                 .arg(DefaultPatternFile));
  const Outcome outcome = replay(xml, pickP());
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  EXPECT_EQ(sourcesOf(outcome.selection), QStringList{"a.archive"});
}

TEST(FomodReplay, UnknownLeafThatMattersIsError) {
  const QString xml = config(
      flagged(R"(<flag name="x">1</flag>)") +
      pattern(
          R"(<dependencies operator="And"><flagDependency flag="x" value="1"/>)"
          R"(<fileDependency file="Other.esp" state="Active"/></dependencies>)"));
  const Outcome outcome = replay(xml, pickP());
  EXPECT_NE(outcome.error.indexOf("pattern #1"), -1)
      << qPrintable(outcome.error);
  EXPECT_NE(outcome.error.indexOf("game version"), -1)
      << qPrintable(outcome.error);
  EXPECT_TRUE(outcome.selection.rules.empty());
}

TEST(FomodReplay, UnknownLeafIrrelevantWhenFlagDecides) {
  // And with a False flag is False whatever the file dependency says.
  const QString decidedFalse = config(
      flagged(R"(<flag name="x">1</flag>)") +
      pattern(
          R"(<dependencies operator="And"><flagDependency flag="x" value="9"/>)"
          R"(<fileDependency file="Other.esp" state="Active"/></dependencies>)"));
  const Outcome skipped = replay(decidedFalse, pickP());
  EXPECT_TRUE(skipped.error.isEmpty()) << qPrintable(skipped.error);
  EXPECT_TRUE(skipped.selection.rules.empty());

  // Or with a True flag is True whatever the file dependency says.
  const QString decidedTrue = config(
      flagged(R"(<flag name="x">1</flag>)") +
      pattern(
          R"(<dependencies operator="Or"><flagDependency flag="x" value="1"/>)"
          R"(<fileDependency file="Other.esp" state="Active"/></dependencies>)"));
  const Outcome applied = replay(decidedTrue, pickP());
  EXPECT_TRUE(applied.error.isEmpty()) << qPrintable(applied.error);
  EXPECT_EQ(sourcesOf(applied.selection), QStringList{"a.archive"});
}

TEST(FomodReplay, GameDependencyIsError) {
  const QString xml = config(
      oneStep(QString()) +
      pattern(
          R"(<dependencies><gameDependency version="2.0"/></dependencies>)"));
  const Outcome outcome = replay(xml, pickP());
  EXPECT_NE(outcome.error.indexOf("pattern #1"), -1)
      << qPrintable(outcome.error);
}

TEST(FomodReplay, EvaluateUsesThreeValuedLogic) {
  const std::map<QString, QString> flags{{"x", "1"}};
  const FomodCondition met = flagCondition("x", "1");
  const FomodCondition unmet = flagCondition("x", "2");
  const FomodCondition unknown = leafCondition(FomodCondition::Kind::File);
  using Kind = FomodCondition::Kind;

  EXPECT_EQ(FomodReplay::evaluate(combine(Kind::And, {unmet, unknown}), flags),
            FomodTriState::False);
  EXPECT_EQ(FomodReplay::evaluate(combine(Kind::And, {met, unknown}), flags),
            FomodTriState::Unknown);
  EXPECT_EQ(FomodReplay::evaluate(combine(Kind::And, {met, met}), flags),
            FomodTriState::True);
  EXPECT_EQ(FomodReplay::evaluate(combine(Kind::Or, {met, unknown}), flags),
            FomodTriState::True);
  EXPECT_EQ(FomodReplay::evaluate(combine(Kind::Or, {unmet, unknown}), flags),
            FomodTriState::Unknown);
  EXPECT_EQ(FomodReplay::evaluate(combine(Kind::Or, {unmet, unmet}), flags),
            FomodTriState::False);
  EXPECT_EQ(FomodReplay::evaluate(combine(Kind::And, {}), flags),
            FomodTriState::True);
  EXPECT_EQ(FomodReplay::evaluate(combine(Kind::Or, {}), flags),
            FomodTriState::False);
  EXPECT_EQ(FomodReplay::evaluate(leafCondition(Kind::Game), flags),
            FomodTriState::Unknown);
  EXPECT_EQ(FomodReplay::evaluate(leafCondition(Kind::Fomm), flags),
            FomodTriState::Unknown);
}

// --- destinations
// ----------------------------------------------------------------

TEST(FomodReplay, FileWithoutDestinationKeepsSourcePath) {
  const Outcome outcome =
      replay(config(oneStep(R"(<file source="dir\x.archive"/>)")), pickP());
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  ASSERT_EQ(outcome.selection.rules.size(), 1u);
  EXPECT_EQ(outcome.selection.rules[0].source, QString("dir/x.archive"));
  EXPECT_EQ(outcome.selection.rules[0].destination, QString("dir/x.archive"));
  EXPECT_FALSE(outcome.selection.rules[0].isFolder);
}

TEST(FomodReplay, FolderWithoutDestinationAtRoot) {
  const Outcome outcome =
      replay(config(oneStep(R"(<folder source="dir"/>)")), pickP());
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  ASSERT_EQ(outcome.selection.rules.size(), 1u);
  EXPECT_TRUE(outcome.selection.rules[0].isFolder);
  EXPECT_EQ(outcome.selection.rules[0].source, QString("dir"));
  EXPECT_EQ(outcome.selection.rules[0].destination, QString(""));
}

TEST(FomodReplay, FolderWithDestinationIsTheTargetDirectory) {
  const Outcome outcome = replay(
      config(oneStep(R"(<folder source="dir" destination="r6\tweaks"/>)")),
      pickP());
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  ASSERT_EQ(outcome.selection.rules.size(), 1u);
  EXPECT_EQ(outcome.selection.rules[0].destination, QString("r6/tweaks"));
}

TEST(FomodReplay, DestinationEndingInSlashIsDirectory) {
  const Outcome outcome = replay(
      config(oneStep(
          R"(<file source="a.archive" destination="archive\pc\mod\"/>)")),
      pickP());
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  ASSERT_EQ(outcome.selection.rules.size(), 1u);
  EXPECT_EQ(outcome.selection.rules[0].destination, QString("archive/pc/mod/"));
}

TEST(FomodReplay, EmptyDestinationAtModRoot) {
  const Outcome empty =
      replay(config(oneStep(R"(<file source="r6\keep.reds" destination=""/>)")),
             pickP());
  ASSERT_TRUE(empty.error.isEmpty()) << qPrintable(empty.error);
  ASSERT_EQ(empty.selection.rules.size(), 1u);
  EXPECT_EQ(empty.selection.rules[0].destination, QString(""));

  const Outcome slashOnly = replay(
      config(oneStep(R"(<file source="r6\keep.reds" destination="\"/>)")),
      pickP());
  ASSERT_TRUE(slashOnly.error.isEmpty()) << qPrintable(slashOnly.error);
  ASSERT_EQ(slashOnly.selection.rules.size(), 1u);
  EXPECT_EQ(slashOnly.selection.rules[0].destination, QString(""));
}

TEST(FomodReplay, PriorityAndSequenceAreCarried) {
  const Outcome outcome =
      replay(config(oneStep(
                 R"(<file source="a.archive" destination="z" priority="5"/>)"
                 R"(<file source="b.archive" destination="z"/>)")),
             pickP());
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  ASSERT_EQ(outcome.selection.rules.size(), 2u);
  EXPECT_EQ(outcome.selection.rules[0].priority, 5);
  EXPECT_EQ(outcome.selection.rules[1].priority, 0);
  EXPECT_LT(outcome.selection.rules[0].sequence,
            outcome.selection.rules[1].sequence);
}

// --- unsafe paths
// ----------------------------------------------------------------

TEST(FomodReplay, RefusesDotDotSourcePath) {
  const Outcome outcome = replay(
      config(oneStep(
          R"(<file source="..\..\etc\passwd" destination="archive\pc\mod\n"/>)")),
      pickP());
  EXPECT_NE(outcome.error.indexOf("unsafe"), -1) << qPrintable(outcome.error);
  EXPECT_TRUE(outcome.selection.rules.empty());
}

TEST(FomodReplay, RefusesDotDotDestinationPath) {
  const Outcome outcome = replay(
      config(oneStep(
          R"(<file source="a.archive" destination="..\..\evil.archive"/>)")),
      pickP());
  EXPECT_NE(outcome.error.indexOf("unsafe"), -1) << qPrintable(outcome.error);
  EXPECT_TRUE(outcome.selection.rules.empty());
}

TEST(FomodReplay, RefusesUnsafePathInRequiredFiles) {
  const Outcome outcome = replay(
      config(
          R"(<requiredInstallFiles><file source="a.archive" destination="..\x"/>)"
          R"(</requiredInstallFiles>)"),
      choiceFor({}));
  EXPECT_NE(outcome.error.indexOf("unsafe"), -1) << qPrintable(outcome.error);
}

TEST(FomodReplay, RefusesEmptySourcePath) {
  const Outcome outcome = replay(
      config(oneStep(R"(<file source="" destination="x.archive"/>)")), pickP());
  EXPECT_NE(outcome.error.indexOf("empty source"), -1)
      << qPrintable(outcome.error);
  EXPECT_TRUE(outcome.selection.rules.empty());
}

TEST(FomodReplay, BackslashPathsSplit) {
  const Outcome outcome =
      replay(config(oneStep(
                 R"(<file source="a\b/c\\d.txt" destination="x\y/z.txt"/>)")),
             pickP());
  ASSERT_TRUE(outcome.error.isEmpty()) << qPrintable(outcome.error);
  ASSERT_EQ(outcome.selection.rules.size(), 1u);
  EXPECT_EQ(outcome.selection.rules[0].source, QString("a/b/c/d.txt"));
  EXPECT_EQ(outcome.selection.rules[0].destination, QString("x/y/z.txt"));
}

TEST(FomodReplay, SafeRelativeNormalisesSeparatorsAndDots) {
  const std::optional<QString> path =
      FomodReplay::safeRelative("a\\b//./c", false);
  ASSERT_TRUE(path.has_value());
  EXPECT_EQ(*path, QString("a/b/c"));

  // A leading separator does not make the path absolute: it is read relative to
  // the root.
  const std::optional<QString> rooted =
      FomodReplay::safeRelative("/etc/x", false);
  ASSERT_TRUE(rooted.has_value());
  EXPECT_EQ(*rooted, QString("etc/x"));
}

TEST(FomodReplay, SafeRelativeRefusesDotDotAndEmptySource) {
  EXPECT_FALSE(FomodReplay::safeRelative("a/../b", true).has_value());
  EXPECT_FALSE(FomodReplay::safeRelative("..", false).has_value());
  EXPECT_FALSE(FomodReplay::safeRelative("", false).has_value());
  EXPECT_FALSE(FomodReplay::safeRelative("./", false).has_value());

  const std::optional<QString> empty = FomodReplay::safeRelative("", true);
  ASSERT_TRUE(empty.has_value());
  EXPECT_TRUE(empty->isEmpty());
}
