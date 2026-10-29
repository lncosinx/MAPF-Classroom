# VENDORED：native/lacam

来源：`third_party/lacam`（Cognitive-AI-Systems/MAPF-GPT `842384e` 的 `lacam/`，MIT；lacam3 原作者 Keisuke Okumura）。

| 文件 | 改动 | 原因 |
|---|---|---|
| `main.cpp` | 删除 `make_log(ins, solution, "lacam_log.txt", ...)` 调用（2 行） | 否则每次求解都在当前目录写 `lacam_log.txt` |
| `CMakeLists.txt` | `target_link_libraries(lacam lacam3 libpthread.so)` → `find_package(Threads REQUIRED)` + `Threads::Threads` | Windows（mingw/MSVC）上没有 `libpthread.so` |
| `lacam3/include/utils.hpp` | `#ifdef _WIN32` 下补 `using uint = unsigned int;` | `uint` 是 glibc `<sys/types.h>` 给的别名，mingw-w64 / MSVC 没有（M7） |
| `lacam3/**` 其余 | 无 | |

编译（Linux）：`cmake -S native/lacam -B build/lacam -DCMAKE_BUILD_TYPE=Release && cmake --build build/lacam -j`，产物 `build/lacam/liblacam.so`。
`main.cpp:20` 的 `log_short` 因此变成未使用变量，编译时有一条无害警告。

交叉编译 Windows 版（M7，在 Linux 上，需 `mingw-w64`）：
`cmake -S native/lacam -B build/lacam-win -DCMAKE_BUILD_TYPE=Release -DCMAKE_TOOLCHAIN_FILE=$PWD/packaging/mingw-w64.cmake && cmake --build build/lacam-win -j`，
产物 `build/lacam-win/liblacam.dll`（打包时改名为 `lacam.dll`）。全静态链接，导入表只有 `KERNEL32.dll` 和 `msvcrt.dll`。
