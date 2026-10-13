cmake_minimum_required(VERSION 3.16)

project(ooz LANGUAGES CXX)

# Kraken decompressor for FromSoftware's DCX archives, loaded through ctypes by
# the Elden Ring plugin. kraken.cpp is upstream's original, kept only to diff
# against kraken_lib.cpp: it defines the same symbols, so it is not compiled.
add_library(ooz SHARED kraken_lib.cpp bitknit.cpp lzna.cpp)

# The root asks for C++23. This is MSVC-era code that has only ever been built
# with g++'s default standard.
set_target_properties(ooz PROPERTIES CXX_STANDARD 17 CXX_STANDARD_REQUIRED ON)

target_compile_options(ooz PRIVATE -O2 -w)

# Nothing in the bundle should depend on the host's libstdc++ version.
target_link_options(ooz PRIVATE -static-libstdc++)

add_library(mo2::ooz ALIAS ooz)
