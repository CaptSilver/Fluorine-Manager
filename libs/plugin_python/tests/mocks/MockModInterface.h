#include <gmock/gmock.h>
#include <uibase/ifiletree.h>
#include <uibase/imodinterface.h>
#include <uibase/versioninfo.h>

using namespace MOBase;

class MockModInterface : public IModInterface {
public:
    // clang-format off
	MOCK_METHOD(QString, name, (), (const, override));
	MOCK_METHOD(QString, absolutePath, (), (const, override));
	MOCK_METHOD(QString, comments, (), (const, override));
	MOCK_METHOD(QString, notes, (), (const, override));
	MOCK_METHOD(QString, gameName, (), (const, override));
	MOCK_METHOD(QString, repository, (), (const, override));
	MOCK_METHOD(int, nexusId, (), (const, override));
	MOCK_METHOD(VersionInfo, version, (), (const, override));
	MOCK_METHOD(VersionInfo, newestVersion, (), (const, override));
	MOCK_METHOD(VersionInfo, ignoredVersion, (), (const, override));
	MOCK_METHOD(QString, installationFile, (), (const, override));
	MOCK_METHOD((std::set<std::pair<int, int>>), installedFiles, (), (const, override));
	MOCK_METHOD(bool, converted, (), (const, override));
	MOCK_METHOD(bool, validated, (), (const, override));
	MOCK_METHOD(QColor, color, (), (const, override));
	MOCK_METHOD(QString, url, (), (const, override));
	MOCK_METHOD(int, primaryCategory, (), (const, override));
	MOCK_METHOD(QStringList, categories, (), (const, override));
	MOCK_METHOD(QString, author, (), (const, override));
	MOCK_METHOD(QString, uploader, (), (const, override));
	MOCK_METHOD(QString, uploaderUrl, (), (const, override));
	MOCK_METHOD(TrackedState, trackedState, (), (const, override));
	MOCK_METHOD(EndorsedState, endorsedState, (), (const, override));
	MOCK_METHOD(std::shared_ptr<const MOBase::IFileTree>, fileTree, (), (const, override));
	MOCK_METHOD(bool, isOverwrite, (), (const, override));
	MOCK_METHOD(bool, isBackup, (), (const, override));
	MOCK_METHOD(bool, isSeparator, (), (const, override));
	MOCK_METHOD(bool, isForeign, (), (const, override));
	MOCK_METHOD(void, setVersion, (const VersionInfo& version), (override));
	MOCK_METHOD(void, setInstallationFile, (const QString& fileName), (override));
	MOCK_METHOD(void, setNewestVersion, (const VersionInfo& version), (override));
	MOCK_METHOD(void, setIsEndorsed, (bool endorsed), (override));
	MOCK_METHOD(void, setNexusID, (int nexusID), (override));
	MOCK_METHOD(void, addNexusCategory, (int categoryID), (override));
	MOCK_METHOD(void, addCategory, (const QString& categoryName), (override));
	MOCK_METHOD(bool, removeCategory, (const QString& categoryName), (override));
	MOCK_METHOD(void, setGameName, (const QString& gameName), (override));
	MOCK_METHOD(void, setUrl, (const QString& url), (override));
	MOCK_METHOD(QVariant, pluginSetting, (const QString& pluginName, const QString& key, const QVariant& defaultValue), (const, override));
	MOCK_METHOD((std::map<QString, QVariant>), pluginSettings, (const QString& pluginName), (const, override));
	MOCK_METHOD(bool, setPluginSetting, (const QString& pluginName, const QString& key, const QVariant& value), (override));
	MOCK_METHOD((std::map<QString, QVariant>), clearPluginSettings, (const QString& pluginName), (override));
    // clang-format on
};
