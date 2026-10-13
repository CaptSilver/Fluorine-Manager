cmake_minimum_required(VERSION 3.16)
project(ModOrganizer2 VERSION 2.5.3 LANGUAGES C CXX)

# ---------------------------------------------------------------------------
# Fluorine Manager version (distinct from the MO2 engine version above).
# ---------------------------------------------------------------------------
file(STRINGS "${CMAKE_SOURCE_DIR}/VERSION" _FLUORINE_VERSION_FILE LIMIT_COUNT 1)
set(FLUORINE_VERSION "${_FLUORINE_VERSION_FILE}" CACHE STRING
    "Fluorine Manager base semantic version")
if(NOT FLUORINE_VERSION MATCHES "^([0-9]+)\\.([0-9]+)\\.([0-9]+)$")
    message(FATAL_ERROR
        "FLUORINE_VERSION must be a semantic version such as 0.3.2; got '${FLUORINE_VERSION}'")
endif()
set(FLUORINE_VERSION_MAJOR "${CMAKE_MATCH_1}")
set(FLUORINE_VERSION_MINOR "${CMAKE_MATCH_2}")
set(FLUORINE_VERSION_PATCH "${CMAKE_MATCH_3}")

set(FLUORINE_BUILD_CHANNEL "dev" CACHE STRING
    "Build channel: 'stable' for tags, 'nightly' for main CI, or 'dev' locally")
set_property(CACHE FLUORINE_BUILD_CHANNEL PROPERTY STRINGS dev nightly stable)
set(FLUORINE_BUILD_NUMBER "" CACHE STRING
    "Monotonically increasing CI run number used to order nightly builds")
set(FLUORINE_BUILD_TIMESTAMP "" CACHE STRING
    "UTC timestamp (YYYYMMDDHHMM) recorded for nightly builds")
set(FLUORINE_BUILD_COMMIT "" CACHE STRING
    "Short git commit SHA for the build, embedded in the About dialog")

if(FLUORINE_BUILD_CHANNEL STREQUAL "nightly")
    set(FLUORINE_IS_NIGHTLY_BUILD 1)
    if(FLUORINE_BUILD_TIMESTAMP STREQUAL "")
        string(TIMESTAMP FLUORINE_BUILD_TIMESTAMP "%Y%m%d%H%M" UTC)
    endif()
    if(FLUORINE_BUILD_NUMBER STREQUAL "")
        set(FLUORINE_BUILD_NUMBER "${FLUORINE_BUILD_TIMESTAMP}")
    endif()
elseif(FLUORINE_BUILD_CHANNEL STREQUAL "stable" OR
       FLUORINE_BUILD_CHANNEL STREQUAL "dev")
    set(FLUORINE_IS_NIGHTLY_BUILD 0)
else()
    message(FATAL_ERROR
        "Unknown FLUORINE_BUILD_CHANNEL '${FLUORINE_BUILD_CHANNEL}'; "
        "expected dev, nightly, or stable")
endif()

if(FLUORINE_BUILD_COMMIT STREQUAL "" AND EXISTS "${CMAKE_SOURCE_DIR}/.git")
    find_program(GIT_EXECUTABLE git)
    if(GIT_EXECUTABLE)
        execute_process(
            COMMAND ${GIT_EXECUTABLE} rev-parse --short HEAD
            WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
            OUTPUT_VARIABLE FLUORINE_BUILD_COMMIT
            OUTPUT_STRIP_TRAILING_WHITESPACE
            ERROR_QUIET)
    endif()
endif()

set(FLUORINE_DISPLAY_VERSION "${FLUORINE_VERSION}")
if(FLUORINE_BUILD_CHANNEL STREQUAL "nightly")
    set(FLUORINE_DISPLAY_VERSION
        "${FLUORINE_VERSION}-nightly.${FLUORINE_BUILD_NUMBER}")
    if(NOT FLUORINE_BUILD_COMMIT STREQUAL "")
        string(APPEND FLUORINE_DISPLAY_VERSION "+${FLUORINE_BUILD_COMMIT}")
    endif()
elseif(FLUORINE_BUILD_CHANNEL STREQUAL "dev")
    set(FLUORINE_DISPLAY_VERSION "${FLUORINE_VERSION}-dev")
    if(NOT FLUORINE_BUILD_COMMIT STREQUAL "")
        string(APPEND FLUORINE_DISPLAY_VERSION "+${FLUORINE_BUILD_COMMIT}")
    endif()
endif()

configure_file(
    "${CMAKE_SOURCE_DIR}/src/src/fluorine_build_info.h.in"
    "${CMAKE_BINARY_DIR}/generated/fluorine_build_info.h"
    @ONLY)
include_directories("${CMAKE_BINARY_DIR}/generated")

message(STATUS "[Fluorine] Version: ${FLUORINE_DISPLAY_VERSION} channel=${FLUORINE_BUILD_CHANNEL} timestamp=${FLUORINE_BUILD_TIMESTAMP} commit=${FLUORINE_BUILD_COMMIT}")

if(POLICY CMP0135)
  cmake_policy(SET CMP0135 NEW)
endif()
if(POLICY CMP0167)
  cmake_policy(SET CMP0167 NEW)
endif()

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Prevent COPY relocations against protected Qt6 symbols on modern distros
# (Fedora 41+, etc.) that set GNU_PROPERTY_1_NEEDED_INDIRECT_EXTERN_ACCESS.
include(CheckCXXCompilerFlag)
check_cxx_compiler_flag(-mno-direct-extern-access HAS_NO_DIRECT_EXTERN_ACCESS)
if(HAS_NO_DIRECT_EXTERN_ACCESS)
    add_compile_options(-mno-direct-extern-access)
endif()

option(BUILD_TESTING "Build tests" OFF)
option(BUILD_FLUORINE_TESTING "Build Fluorine standalone tests" OFF)
option(BUILD_PLUGIN_PYTHON_TESTS "Build native plugin_python bridge tests" OFF)
option(BUILD_BSA_FFI "Build Rust BSA/BA2 FFI library" ON)
option(BUILD_DOTNET_PLUGINS "Build C++/CLI .NET plugins (Windows+MSVC only)" OFF)
option(BUILD_PLUGIN_PYTHON "Build plugin_python bridge on Linux" ON)

if(BUILD_PLUGIN_PYTHON_TESTS AND NOT BUILD_PLUGIN_PYTHON)
    message(FATAL_ERROR "BUILD_PLUGIN_PYTHON_TESTS requires BUILD_PLUGIN_PYTHON=ON")
endif()

if (BUILD_TESTING OR BUILD_FLUORINE_TESTING OR BUILD_PLUGIN_PYTHON_TESTS)
    enable_testing()
endif()

# Compatibility helpers for upstream MO2 plugin repos that expect mo2-cmake.
if(NOT COMMAND mo2_configure_plugin)
  function(mo2_configure_plugin target)
    cmake_parse_arguments(MO2 "NO_SOURCES" "" "" ${ARGN})

    set_target_properties(${target} PROPERTIES
      AUTOMOC ON
      AUTOUIC ON
      AUTORCC ON
      CXX_STANDARD 23
      CXX_STANDARD_REQUIRED ON
      POSITION_INDEPENDENT_CODE ON
    )

    if(NOT MO2_NO_SOURCES)
      file(GLOB _mo2_plugin_sources CONFIGURE_DEPENDS
        ${CMAKE_CURRENT_SOURCE_DIR}/*.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/*.h
        ${CMAKE_CURRENT_SOURCE_DIR}/*.ui
        ${CMAKE_CURRENT_SOURCE_DIR}/*.qrc
      )
      if(_mo2_plugin_sources)
        target_sources(${target} PRIVATE ${_mo2_plugin_sources})
      endif()
    endif()

    target_compile_options(${target} PRIVATE
      "-idirafter" "${CMAKE_SOURCE_DIR}/libs/uibase/include/uibase"
    )
    set_property(TARGET ${target} APPEND PROPERTY AUTOMOC_MOC_OPTIONS
      "-I${CMAKE_SOURCE_DIR}/libs/uibase/include/uibase"
    )
  endfunction()
endif()

if(NOT COMMAND mo2_install_plugin)
  function(mo2_install_plugin target)
    install(TARGETS ${target}
      LIBRARY DESTINATION plugins
    )
  endfunction()
endif()

if(NOT COMMAND mo2_default_source_group)
  function(mo2_default_source_group)
    # no-op
  endfunction()
endif()

if(NOT COMMAND mo2_configure_target)
  function(mo2_configure_target target)
    set_target_properties(${target} PROPERTIES
      AUTOMOC ON
      AUTOUIC ON
      AUTORCC ON
      CXX_STANDARD 23
      CXX_STANDARD_REQUIRED ON
      POSITION_INDEPENDENT_CODE ON
    )
    target_compile_options(${target} PRIVATE
      "-idirafter" "${CMAKE_SOURCE_DIR}/libs/uibase/include/uibase"
    )
    set_property(TARGET ${target} APPEND PROPERTY AUTOMOC_MOC_OPTIONS
      "-I${CMAKE_SOURCE_DIR}/libs/uibase/include/uibase"
    )
  endfunction()
endif()

if(NOT COMMAND mo2_target_sources)
  function(mo2_target_sources target)
    set(_scope PRIVATE)
    set(_out)
    set(_skip_next OFF)
    foreach(_arg IN LISTS ARGN)
      if(_skip_next)
        set(_skip_next OFF)
        continue()
      endif()
      if(_arg STREQUAL "FOLDER")
        set(_skip_next ON)
        continue()
      endif()
      if(_arg STREQUAL "PRIVATE" OR _arg STREQUAL "PUBLIC" OR _arg STREQUAL "INTERFACE")
        set(_scope "${_arg}")
        continue()
      endif()
      list(APPEND _out "${_arg}")
    endforeach()
    if(_out)
      target_sources(${target} ${_scope} ${_out})
    endif()
  endfunction()
endif()

if(NOT COMMAND mo2_find_python_executable)
  function(mo2_find_python_executable out_var)
    if(DEFINED Python_EXECUTABLE AND Python_EXECUTABLE)
      set(${out_var} "${Python_EXECUTABLE}" PARENT_SCOPE)
      return()
    endif()
    find_program(_mo2_python_exe NAMES python3 python)
    set(${out_var} "${_mo2_python_exe}" PARENT_SCOPE)
  endfunction()
endif()

if(NOT COMMAND mo2_add_translations)
  function(mo2_add_translations)
    # no-op on Linux port for now
  endfunction()
endif()

if(NOT COMMAND mo2_python_install_pyqt)
  function(mo2_python_install_pyqt)
    # Use system/venv PyQt6 directly; no bundled wheel install step.
  endfunction()
endif()

# Make dds-header findable by bsatk and libbsarch
set(mo2-dds-header_DIR "${CMAKE_CURRENT_SOURCE_DIR}/libs/dds-header")

# Sub-libraries in dependency order
add_subdirectory(libs/esptk)      # static lib, no deps
add_subdirectory(libs/bsatk)      # static lib, needs dds-header + boost + zlib + lz4
add_subdirectory(libs/libbsarch)  # shared + static, needs dds-header
add_subdirectory(libs/7zip)        # builds 7z.so from source (Linux only)
add_subdirectory(libs/archive)    # shared lib, needs dl
add_subdirectory(libs/ooz)        # shared lib, libc only

add_subdirectory(libs/uibase)     # shared lib, needs Qt6 + spdlog

if(BUILD_BSA_FFI)
    add_subdirectory(libs/bsa_ffi)
endif()

add_subdirectory(libs/steam_appinfo_ffi)

if(BUILD_PLUGIN_PYTHON)
  # If an explicit Python executable was provided (Docker/CI), use it directly.
  # Otherwise, bootstrap a local venv with uv for source builds.
  if(NOT DEFINED Python_EXECUTABLE OR NOT Python_EXECUTABLE)
    set(_mo2_pyvenv "${CMAKE_BINARY_DIR}/pyvenv")
    set(_mo2_pyvenv_python "${_mo2_pyvenv}/bin/python")

    if(NOT EXISTS "${_mo2_pyvenv_python}")
      find_program(_uv_exe uv REQUIRED)
      message(STATUS "Bootstrapping Python venv with uv...")
      execute_process(
        COMMAND ${_uv_exe} venv "${_mo2_pyvenv}" --python 3.13 --seed
        RESULT_VARIABLE _uv_venv_rc)
      if(NOT _uv_venv_rc EQUAL 0)
        message(FATAL_ERROR
          "uv venv creation failed (rc=${_uv_venv_rc}). "
          "Install uv: https://docs.astral.sh/uv/")
      endif()
      execute_process(
        COMMAND ${_uv_exe} pip install --python "${_mo2_pyvenv_python}"
                pybind11==2.13.6 sip psutil vdf
        RESULT_VARIABLE _uv_pip_rc)
      if(NOT _uv_pip_rc EQUAL 0)
        message(FATAL_ERROR "uv pip install failed (rc=${_uv_pip_rc})")
      endif()
    endif()

    set(Python_EXECUTABLE "${_mo2_pyvenv_python}" CACHE FILEPATH
      "Python executable for plugin_python build" FORCE)
    # Detect pybind11 cmake dir dynamically.
    execute_process(
      COMMAND "${_mo2_pyvenv_python}" -c
        "import pybind11; print(pybind11.get_cmake_dir())"
      OUTPUT_VARIABLE _pybind11_cmake_dir
      OUTPUT_STRIP_TRAILING_WHITESPACE
      RESULT_VARIABLE _pybind11_rc)
    if(_pybind11_rc EQUAL 0 AND _pybind11_cmake_dir)
      set(pybind11_DIR "${_pybind11_cmake_dir}"
        CACHE PATH "pybind11 cmake dir for plugin_python" FORCE)
    endif()
    set(MO2_PYLIBS_DIR "${_mo2_pyvenv}"
      CACHE PATH "Python runtime root for plugin_python" FORCE)
  endif()

  add_subdirectory(libs/plugin_python)
endif()

# Game plugin chain: features (headers) → gamebryo (static) → creation (static) → game .so plugins
add_subdirectory(libs/game_features)
add_subdirectory(libs/game_bethesda)
add_subdirectory(libs/installer_fomod)
add_subdirectory(libs/installer_bsplugins)
add_subdirectory(libs/installer_bain)
add_subdirectory(libs/installer_bundle)
add_subdirectory(libs/installer_manual)
add_subdirectory(libs/installer_quick)
add_subdirectory(libs/preview_base)
add_subdirectory(libs/preview_bsa)
add_subdirectory(libs/preview_nif)
add_subdirectory(libs/bsa_extractor)
add_subdirectory(libs/bsapacker)
add_subdirectory(libs/diagnose_basic)
add_subdirectory(libs/check_fnis)
add_subdirectory(libs/skse_log_redirector)
add_subdirectory(libs/tool_inibakery)
add_subdirectory(libs/tool_inieditor)
add_subdirectory(libs/tool_bethinipie)
if(BUILD_DOTNET_PLUGINS)
  if(WIN32 AND MSVC)
    add_subdirectory(libs/installer_fomod_csharp)
    add_subdirectory(libs/installer_omod)
  else()
    message(WARNING
      "BUILD_DOTNET_PLUGINS was enabled, but installer_fomod_csharp/installer_omod "
      "are C++/CLI targets that require Windows + MSVC. Skipping on this platform."
    )
  endif()
endif()

# Main organizer executable
add_subdirectory(src/src)

if (BUILD_FLUORINE_TESTING)
    add_subdirectory(src/tests)
endif()

if (BUILD_PLUGIN_PYTHON_TESTS)
    if (NOT TARGET fluorine-tests)
        add_custom_target(fluorine-tests)
    endif()
    add_dependencies(fluorine-tests runner-tests python-tests)
endif()
