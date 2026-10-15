#include "collections/fomodmodel.h"

#include <gtest/gtest.h>

#include <QString>
#include <QStringConverter>

#include <ostream>

[[maybe_unused]] static void PrintTo(const QString &text, std::ostream *os) {
  *os << '"' << text.toStdString() << '"';
}

namespace {
using Kind = FomodCondition::Kind;

// ModuleConfig.xml of a car mod: required files, one step with a group of three
// plugins (one sets a flag), and a pattern that adds a file when the flag is
// set. Paths use backslashes the way Windows-authored installers do.
const char *const CarXml = R"xml(<config>
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
</config>)xml";

QByteArray utf16le(const QString &text) {
  QByteArray out("\xff\xfe", 2);
  for (QChar c : text) {
    out.append(static_cast<char>(c.unicode() & 0xff));
    out.append(static_cast<char>(c.unicode() >> 8));
  }
  return out;
}

FomodModel parseOrFail(const QByteArray &xml) {
  QString error;
  const std::optional<FomodModel> model = FomodModel::parse(xml, error);
  EXPECT_TRUE(model.has_value()) << error.toStdString();
  return model.value_or(FomodModel{});
}
} // namespace

TEST(FomodModel, ParsesStepsGroupsPluginsInDocumentOrder) {
  const FomodModel model = parseOrFail(CarXml);

  ASSERT_EQ(model.steps.size(), 1u);
  EXPECT_EQ(model.steps[0].name, "Parts");
  ASSERT_EQ(model.steps[0].groups.size(), 1u);
  const FomodGroup &group = model.steps[0].groups[0];
  EXPECT_EQ(group.name, "Spoiler");
  EXPECT_EQ(group.type, "SelectExactlyOne");
  ASSERT_EQ(group.plugins.size(), 3u);
  EXPECT_EQ(group.plugins[0].name, "none");
  EXPECT_EQ(group.plugins[1].name, "sp01");
  EXPECT_EQ(group.plugins[2].name, "sp02");
  EXPECT_EQ(group.plugins[0].documentIndex, 0);
  EXPECT_EQ(group.plugins[2].documentIndex, 2);
  EXPECT_TRUE(group.plugins[0].files.empty());

  ASSERT_EQ(group.plugins[1].files.size(), 1u);
  EXPECT_EQ(group.plugins[1].files[0].source, "opt\\sp01.archive");
  EXPECT_EQ(group.plugins[1].files[0].destination,
            "archive\\pc\\mod\\sp01.archive");
  ASSERT_EQ(group.plugins[1].flags.size(), 1u);
  EXPECT_EQ(group.plugins[1].flags[0].name, "spoiler");
  EXPECT_EQ(group.plugins[1].flags[0].value, "on");
  EXPECT_TRUE(group.plugins[2].flags.empty());
}

TEST(FomodModel, KeepsDocumentOrderAcrossStepsAndGroups) {
  const FomodModel model = parseOrFail(R"xml(<config><installSteps>
    <installStep name="Two"><optionalFileGroups>
      <group name="B"><plugins><plugin name="z"/><plugin name="a"/></plugins></group>
      <group name="A"><plugins><plugin name="m"/></plugins></group>
    </optionalFileGroups></installStep>
    <installStep name="One"><optionalFileGroups>
      <group name="C"><plugins><plugin name="q"/></plugins></group>
    </optionalFileGroups></installStep>
  </installSteps></config>)xml");

  ASSERT_EQ(model.steps.size(), 2u);
  EXPECT_EQ(model.steps[0].name, "Two");
  EXPECT_EQ(model.steps[1].name, "One");
  ASSERT_EQ(model.steps[0].groups.size(), 2u);
  EXPECT_EQ(model.steps[0].groups[0].name, "B");
  EXPECT_EQ(model.steps[0].groups[1].name, "A");
  ASSERT_EQ(model.steps[0].groups[0].plugins.size(), 2u);
  EXPECT_EQ(model.steps[0].groups[0].plugins[0].name, "z");
  EXPECT_EQ(model.steps[0].groups[0].plugins[1].name, "a");
  EXPECT_EQ(model.steps[0].groups[0].plugins[1].documentIndex, 1);
  EXPECT_EQ(model.steps[0].groups[1].plugins[0].documentIndex, 0);
}

TEST(FomodModel, ParsesTypeDescriptorWithoutUsingIt) {
  const FomodModel model =
      parseOrFail(R"xml(<config><installSteps><installStep name="S">
    <optionalFileGroups><group name="G"><plugins>
      <plugin name="direct"><typeDescriptor><type name="Required"/></typeDescriptor></plugin>
      <plugin name="dependent"><typeDescriptor><dependencyType>
        <defaultType name="Recommended"/></dependencyType></typeDescriptor></plugin>
      <plugin name="none"/>
    </plugins></group></optionalFileGroups></installStep></installSteps></config>)xml");

  const std::vector<FomodPlugin> &plugins = model.steps[0].groups[0].plugins;
  ASSERT_EQ(plugins.size(), 3u);
  EXPECT_EQ(plugins[0].typeDescriptor, "Required");
  EXPECT_EQ(plugins[1].typeDescriptor, "Recommended");
  EXPECT_TRUE(plugins[2].typeDescriptor.isEmpty());
}

TEST(FomodModel, ParsesRequiredInstallFiles) {
  const FomodModel model = parseOrFail(CarXml);

  ASSERT_EQ(model.requiredInstallFiles.size(), 1u);
  const FomodFile &file = model.requiredInstallFiles[0];
  EXPECT_EQ(file.source, "core");
  EXPECT_EQ(file.destination, "archive\\pc\\mod");
  EXPECT_TRUE(file.hasDestination);
  EXPECT_TRUE(file.isFolder);
  EXPECT_EQ(file.priority, 0);
}

TEST(FomodModel, ParsesConditionalPatternsWithNestedAndOr) {
  const FomodModel model =
      parseOrFail(R"xml(<config><conditionalFileInstalls><patterns>
    <pattern>
      <dependencies operator="Or">
        <flagDependency flag="a" value="1"/>
        <dependencies operator="And">
          <flagDependency flag="b" value="2"/>
          <flagDependency flag="c" value="3"/>
        </dependencies>
      </dependencies>
      <files><file source="x" destination="y" priority="4"/></files>
    </pattern>
    <pattern><files><folder source="z"/></files></pattern>
  </patterns></conditionalFileInstalls></config>)xml");

  ASSERT_EQ(model.conditionalFileInstalls.size(), 2u);

  const FomodPattern &nested = model.conditionalFileInstalls[0];
  EXPECT_EQ(nested.condition.kind, Kind::Or);
  ASSERT_EQ(nested.condition.children.size(), 2u);
  EXPECT_EQ(nested.condition.children[0].kind, Kind::Flag);
  EXPECT_EQ(nested.condition.children[0].name, "a");
  EXPECT_EQ(nested.condition.children[0].value, "1");
  const FomodCondition &inner = nested.condition.children[1];
  EXPECT_EQ(inner.kind, Kind::And);
  ASSERT_EQ(inner.children.size(), 2u);
  EXPECT_EQ(inner.children[0].name, "b");
  EXPECT_EQ(inner.children[0].value, "2");
  EXPECT_EQ(inner.children[1].name, "c");
  EXPECT_EQ(inner.children[1].value, "3");
  ASSERT_EQ(nested.files.size(), 1u);
  EXPECT_EQ(nested.files[0].priority, 4);

  // No <dependencies> at all: an And with nothing to fail, i.e. always applies.
  const FomodPattern &bare = model.conditionalFileInstalls[1];
  EXPECT_EQ(bare.condition.kind, Kind::And);
  EXPECT_TRUE(bare.condition.children.empty());
  ASSERT_EQ(bare.files.size(), 1u);
  EXPECT_TRUE(bare.files[0].isFolder);
  EXPECT_FALSE(bare.files[0].hasDestination);
}

TEST(FomodModel, FileGameFommLeavesParsed) {
  const FomodModel model =
      parseOrFail(R"xml(<config><conditionalFileInstalls><patterns>
    <pattern><dependencies operator="And">
      <fileDependency file="a.esp" state="Active"/>
      <gameDependency version="1.2"/>
      <fommDependency version="3"/>
    </dependencies><files/></pattern>
  </patterns></conditionalFileInstalls></config>)xml");

  ASSERT_EQ(model.conditionalFileInstalls.size(), 1u);
  const std::vector<FomodCondition> &leaves =
      model.conditionalFileInstalls[0].condition.children;
  ASSERT_EQ(leaves.size(), 3u);
  EXPECT_EQ(leaves[0].kind, Kind::File);
  EXPECT_EQ(leaves[0].name, "a.esp");
  EXPECT_EQ(leaves[0].value, "Active");
  EXPECT_EQ(leaves[1].kind, Kind::Game);
  EXPECT_EQ(leaves[1].value, "1.2");
  EXPECT_EQ(leaves[2].kind, Kind::Fomm);
  EXPECT_EQ(leaves[2].value, "3");
}

TEST(FomodModel, TagsAndAttributesCaseInsensitive) {
  const FomodModel model = parseOrFail(R"xml(<CONFIG>
    <REQUIREDINSTALLFILES><FILE SOURCE="a" DESTINATION="b" PRIORITY="2"/></REQUIREDINSTALLFILES>
    <InstallSteps><INSTALLSTEP NAME="S"><OptionalFileGroups><GROUP NAME="G" TYPE="SelectAny"><PLUGINS>
      <Plugin Name="P"><Files><File Source="s"/></Files>
        <ConditionFlags><Flag Name="f">v</Flag></ConditionFlags></Plugin>
    </PLUGINS></GROUP></OptionalFileGroups></INSTALLSTEP></InstallSteps>
    <ConditionalFileInstalls><Patterns><Pattern><Dependencies Operator="OR">
      <FlagDependency Flag="f" Value="v"/></Dependencies><Files/></Pattern></Patterns>
    </ConditionalFileInstalls></CONFIG>)xml");

  ASSERT_EQ(model.requiredInstallFiles.size(), 1u);
  EXPECT_EQ(model.requiredInstallFiles[0].source, "a");
  EXPECT_EQ(model.requiredInstallFiles[0].destination, "b");
  EXPECT_EQ(model.requiredInstallFiles[0].priority, 2);
  ASSERT_EQ(model.steps.size(), 1u);
  EXPECT_EQ(model.steps[0].name, "S");
  EXPECT_EQ(model.steps[0].groups[0].type, "SelectAny");
  const FomodPlugin &plugin = model.steps[0].groups[0].plugins[0];
  EXPECT_EQ(plugin.name, "P");
  ASSERT_EQ(plugin.files.size(), 1u);
  EXPECT_EQ(plugin.files[0].source, "s");
  ASSERT_EQ(plugin.flags.size(), 1u);
  EXPECT_EQ(plugin.flags[0].name, "f");
  EXPECT_EQ(plugin.flags[0].value, "v");
  ASSERT_EQ(model.conditionalFileInstalls.size(), 1u);
  EXPECT_EQ(model.conditionalFileInstalls[0].condition.kind, Kind::Or);
  EXPECT_EQ(model.conditionalFileInstalls[0].condition.children[0].name, "f");
}

TEST(FomodModel, Utf16BomEncodingParsed) {
  const QString text = QString("<?xml version=\"1.0\" encoding=\"UTF-16\"?>") +
                       QString::fromUtf8(CarXml);
  const QByteArray bytes = utf16le(text);

  EXPECT_TRUE(decodeFomodXml(bytes).startsWith("<config>"));
  const FomodModel model = parseOrFail(bytes);
  ASSERT_EQ(model.steps.size(), 1u);
  EXPECT_EQ(model.steps[0].groups[0].plugins.size(), 3u);
}

TEST(FomodModel, Utf8BomWithWrongDeclarationParsed) {
  const QByteArray bytes = QByteArray("\xef\xbb\xbf") +
                           "<?xml version=\"1.0\" encoding=\"utf-16\"?>" +
                           CarXml;

  EXPECT_TRUE(decodeFomodXml(bytes).startsWith("<config>"));
  const FomodModel model = parseOrFail(bytes);
  ASSERT_EQ(model.steps.size(), 1u);
  EXPECT_EQ(model.steps[0].groups[0].plugins[1].name, "sp01");
}

TEST(FomodModel, Cp1252WithoutBomEncodingParsed) {
  // 0xE9 is e-acute in cp1252 and Latin-1, and is not valid UTF-8 here.
  const QByteArray bytes =
      "<config><installSteps><installStep name=\"Caf\xe9\"><optionalFileGroups>"
      "</optionalFileGroups></installStep></installSteps></config>";

  const FomodModel model = parseOrFail(bytes);

  ASSERT_EQ(model.steps.size(), 1u);
  EXPECT_EQ(model.steps[0].name, QString::fromUtf8("Caf\xc3\xa9"));
}

TEST(FomodModel, Cp1252SpecificBytesDecodeWhenCodecAvailable) {
  QStringDecoder codec("windows-1252");
  if (!codec.isValid())
    GTEST_SKIP()
        << "Qt has no windows-1252 codec; decoding falls back to Latin-1";

  // 0x92 is a right single quote in cp1252 and a control character in Latin-1.
  const QByteArray bytes =
      "<config><installSteps><installStep name=\"It\x92s\"><optionalFileGroups>"
      "</optionalFileGroups></installStep></installSteps></config>";

  const FomodModel model = parseOrFail(bytes);

  ASSERT_EQ(model.steps.size(), 1u);
  EXPECT_EQ(model.steps[0].name, QString::fromUtf8("It\xe2\x80\x99s"));
}

TEST(FomodModel, MalformedXmlIsError) {
  for (const QByteArray &bad : {QByteArray("<config><installSteps>"),
                                QByteArray("<config><a></b></config>"),
                                QByteArray(), QByteArray("not xml at all")}) {
    QString error;
    EXPECT_FALSE(FomodModel::parse(bad, error).has_value())
        << bad.toStdString();
    EXPECT_TRUE(error.contains("ModuleConfig.xml")) << error.toStdString();
  }
}

TEST(FomodModel, BadPriorityIsError) {
  QString error;
  const auto model = FomodModel::parse(
      R"xml(<config><requiredInstallFiles><file source="a" priority="high"/>
            </requiredInstallFiles></config>)xml",
      error);

  EXPECT_FALSE(model.has_value());
  EXPECT_TRUE(error.contains("priority")) << error.toStdString();
}

TEST(FomodModel, DestinationPresenceRecorded) {
  const FomodModel model = parseOrFail(R"xml(<config><requiredInstallFiles>
    <file source="a"/>
    <file source="b" destination=""/>
    <file source="c" destination="d\"/>
  </requiredInstallFiles></config>)xml");

  ASSERT_EQ(model.requiredInstallFiles.size(), 3u);
  EXPECT_FALSE(model.requiredInstallFiles[0].hasDestination);
  EXPECT_TRUE(model.requiredInstallFiles[0].destination.isEmpty());
  EXPECT_TRUE(model.requiredInstallFiles[1].hasDestination);
  EXPECT_TRUE(model.requiredInstallFiles[1].destination.isEmpty());
  EXPECT_TRUE(model.requiredInstallFiles[2].hasDestination);
  EXPECT_EQ(model.requiredInstallFiles[2].destination, "d\\");
  EXPECT_FALSE(model.requiredInstallFiles[2].isFolder);
}
