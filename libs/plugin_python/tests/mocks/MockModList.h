#include <gmock/gmock.h>
#include <uibase/imodlist.h>

using namespace MOBase;

class MockModList : public IModList {
public:
    // clang-format off
	MOCK_METHOD(QString, displayName, (const QString& internalName), (const, override));
	MOCK_METHOD(QStringList, allMods, (), (const, override));
	MOCK_METHOD(QStringList, allModsByProfilePriority, (MOBase::IProfile* profile), (const, override));
	MOCK_METHOD(IModInterface*, getMod, (const QString& name), (const, override));
	MOCK_METHOD(bool, removeMod, (MOBase::IModInterface* mod), (override));
	MOCK_METHOD(MOBase::IModInterface*, renameMod, (MOBase::IModInterface* mod, const QString& name), (override));
	MOCK_METHOD(ModStates, state, (const QString& name), (const, override));
	MOCK_METHOD(bool, setActive, (const QString& name, bool active), (override));
	MOCK_METHOD(int, setActive, (const QStringList& names, bool active), (override));
	MOCK_METHOD(int, priority, (const QString& name), (const, override));
	MOCK_METHOD(bool, setPriority, (const QString& name, int newPriority), (override));
	MOCK_METHOD(bool, onModInstalled, (const std::function<void(IModInterface*)>& func), (override));
	MOCK_METHOD(bool, onModRemoved, (const std::function<void(QString const&)>& func), (override));
	MOCK_METHOD(bool, onModStateChanged, (const std::function<void(const std::map<QString, ModStates>&)>& func), (override));
	MOCK_METHOD(bool, onModMoved, (const std::function<void(const QString&, int, int)>& func), (override));
	MOCK_METHOD(bool, addContextMenuAction, (const QString& label, const std::function<void(IModInterface*, QWidget*)>& triggered, const std::function<bool(IModInterface*)>& visible), (override));
    // clang-format on
};
