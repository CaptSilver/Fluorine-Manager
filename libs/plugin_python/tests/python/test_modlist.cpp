#include "pybind11_qt/pybind11_qt.h"

#include <pybind11/functional.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <exception>
#include <functional>
#include <string>
#include <vector>

#include <QString>
#include <QWidget>

#include "MockModInterface.h"
#include "MockModList.h"

namespace py = pybind11;
using namespace pybind11::literals;
using ::testing::NiceMock;

namespace {
struct Registered {
    QString label;
    std::function<void(IModInterface*, QWidget*)> triggered;
    std::function<bool(IModInterface*)> visible;
};

std::vector<Registered> s_Registered;
}  // namespace

PYBIND11_MODULE(modlist, m)
{
    // The stored callables hold Python references, and destroying them needs the
    // interpreter. Static destruction runs after it is gone, so empty the list at
    // exit; the tests also call reset() after each case.
    py::module_::import("atexit").attr("register")(
        py::cpp_function([] {
            s_Registered.clear();
        }));

    m.def("reset", [] {
        s_Registered.clear();
    });

    m.def("modlist", []() -> IModList* {
        s_Registered.clear();

        MockModList* mock = new NiceMock<MockModList>();
        ON_CALL(*mock, addContextMenuAction)
            .WillByDefault([](const QString& label, const auto& triggered,
                              const auto& visible) {
                s_Registered.push_back({label, triggered, visible});
                return true;
            });
        return mock;
    });

    m.def("mod", []() -> IModInterface* {
        return new NiceMock<MockModInterface>();
    });

    m.def("labels", []() {
        std::vector<QString> out;
        for (const auto& r : s_Registered) {
            out.push_back(r.label);
        }
        return out;
    });

    // an empty visible callback means the entry is always shown
    m.def("has_visible", [](std::size_t i) {
        return static_cast<bool>(s_Registered.at(i).visible);
    });

    m.def("visible", [](std::size_t i, IModInterface* mod) {
        const auto& visible = s_Registered.at(i).visible;
        return visible ? visible(mod) : true;
    });

    m.def("trigger", [](std::size_t i, IModInterface* mod, QWidget* parent) {
        s_Registered.at(i).triggered(mod, parent);
    });

    // what the proxy's catch sees when a Python callback raises
    m.def("visible_error", [](std::size_t i, IModInterface* mod) -> std::string {
        try {
            s_Registered.at(i).visible(mod);
        }
        catch (const std::exception& e) {
            return e.what();
        }
        return {};
    });

    m.def("trigger_error",
          [](std::size_t i, IModInterface* mod, QWidget* parent) -> std::string {
              try {
                  s_Registered.at(i).triggered(mod, parent);
              }
              catch (const std::exception& e) {
                  return e.what();
              }
              return {};
          });
}
