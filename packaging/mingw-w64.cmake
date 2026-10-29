# 在 Linux 上用 mingw-w64 交叉编译 Windows 版 lacam.dll（M7）。
# 必须用 posix 线程版编译器：LaCAM 用到 std::thread，win32 线程版的 libstdc++ 没有它。
# 全部静态链接：dll 只依赖 Windows 系统库，不依赖 libstdc++-6.dll / libwinpthread-1.dll / libgcc_s_seh-1.dll。
set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR x86_64)
set(CMAKE_C_COMPILER x86_64-w64-mingw32-gcc-posix)
set(CMAKE_CXX_COMPILER x86_64-w64-mingw32-g++-posix)
set(CMAKE_RC_COMPILER x86_64-w64-mingw32-windres)
set(CMAKE_FIND_ROOT_PATH /usr/x86_64-w64-mingw32)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_SHARED_LINKER_FLAGS_INIT "-static -static-libgcc -static-libstdc++")
