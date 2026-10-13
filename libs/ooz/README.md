# ooz (vendored)

Kraken/Mermaid/Selkie/Leviathan/LZNA/BitKnit decompressor from
https://github.com/powzix/ooz, Copyright (C) 2016 Powzix, GPL-3.0-or-later.
`kraken_lib.cpp` and `kraken.cpp` keep the
upstream notice at the top; `bitknit.cpp` and `lzna.cpp` have none upstream
and are covered by this one.

Vendored rather than submoduled because we have to modify it. A submodule
would pin someone else's tree, so carrying these patches would mean
maintaining a fork or a build-time patch step that can fail.

## Local modifications

**`compat.h`**: ooz is MSVC-flavored and will not compile under g++ without
shims for `_BitScanReverse`, `_BitScanForward`, `__forceinline`, and several
MSVC typedefs. GCC's `x86intrin.h` supplies `_rotl`.

**`kraken.cpp`**: upstream's original, unmodified and deliberately not built.
It exists so `diff kraken.cpp kraken_lib.cpp` shows exactly what we changed.
Adding it to the target produces duplicate-symbol link errors.

**`kraken_lib.cpp`**: adds `ooz_decompress_seekchunked`, the only entry point we
export and call. `Kraken_ParseHeader` also refuses LZNA (type 5) and BitKnit
(type 11) streams: their decoders do no bounds checking, the input is
untrusted mod content, and nothing FromSoftware ships uses them.
Upstream says the decoder as a whole is "not fuzz safe", and the Kraken-family
decoders that remain are no more hardened: they run in-process on untrusted mod
archives, so a crafted archive can still crash the process. Decoding out of
process would be the real fix.

**`stdafx.h`**: every vendored `.cpp` still `#include`s this (an MSVC
precompiled-header convention). Upstream's version pulls in `<Windows.h>` and
`<tchar.h>`, so it's replaced with a two-line shim that just forwards to
`compat.h`.

## The seek-chunk gotcha

Stock `Kraken_Decompress` fails on **every** FromSoftware DCX file, vanilla
included, after exactly one 256 KB block. This is not a build error: a correct
build decodes the whole Silesia corpus byte-perfect, including a 51 MB file.

FromSoft sets `seekChunkReset=true`, so each 256 KB chunk is coded
independently and back-references must be chunk-relative. The fix is to loop
over `Kraken_DecodeStep` with a per-chunk destination base rather than calling
`Kraken_Decompress` once:

    if (!Kraken_DecodeStep(dec, dst + written, 0, dst_len, src, src_len)) return -1;

If you are debugging a decode that dies after 262144 bytes, this is why.

## Build and loading

`CMakeLists.txt` builds `libooz.so` from `kraken_lib.cpp`, `bitknit.cpp` and
`lzna.cpp` (C++17, `-O2`, libstdc++ linked statically so the bundle needs no
particular C++ runtime). `build-inner.sh` copies it to `lib/` in the bundle.

The Elden Ring plugin loads it through ctypes in
`games/eldenring_support/ermlib/formats/ooz.py`: by the bare soname, which the
launcher's `LD_LIBRARY_PATH` resolves to the bundle's `lib/`, or from the path
in `FLUORINE_OOZ_LIB` when the test suite runs out of the build tree.
