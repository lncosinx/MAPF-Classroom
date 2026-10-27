# VENDORED：native/lacam

来源：`third_party/lacam`（Cognitive-AI-Systems/MAPF-GPT `842384e` 的 `lacam/`，MIT；lacam3 原作者 Keisuke Okumura）。

| 文件 | 改动 | 原因 |
|---|---|---|
| `main.cpp` | 删除 `make_log(ins, solution, "lacam_log.txt", ...)` 调用（2 行） | 否则每次求解都在当前目录写 `lacam_log.txt` |
| `CMakeLists.txt` | `target_link_libraries(lacam lacam3 libpthread.so)` → `find_package(Threads REQUIRED)` + `Threads::Threads` | Windows（mingw/MSVC）上没有 `libpthread.so` |
| `lacam3/**` | 无 | |

编译（Linux）：`cmake -S native/lacam -B build/lacam -DCMAKE_BUILD_TYPE=Release && cmake --build build/lacam -j`，产物 `build/lacam/liblacam.so`。
`main.cpp:20` 的 `log_short` 因此变成未使用变量，编译时有一条无害警告。
