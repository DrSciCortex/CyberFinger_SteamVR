cmake_minimum_required(VERSION 3.16)
project(driver_cyberfinger VERSION 2.2.5 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Static C/C++ runtime (/MT): SteamVR does not ship the VC++ 2015-2022 runtime, so a /MD driver
# would fail to load on PCs without the redistributable.
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

# ── OpenVR SDK ──────────────────────────────────────────────────────────────
# Set OPENVR_SDK to the root of your openvr checkout (git submodule update --init)
if(NOT DEFINED OPENVR_SDK)
    set(OPENVR_SDK "${CMAKE_SOURCE_DIR}/openvr" CACHE PATH "Path to OpenVR SDK")
endif()

if(NOT EXISTS "${OPENVR_SDK}/headers/openvr_driver.h")
    message(FATAL_ERROR
        "OpenVR SDK not found at ${OPENVR_SDK}. "
        "Run 'git submodule update --init' or set -DOPENVR_SDK=<path>")
endif()

# ── MinHook (Windows): observes Steam Link's hand skeletons inside vrserver ──
set(MINHOOK_DIR "${CMAKE_SOURCE_DIR}/third_party/minhook")
if(WIN32)
    if(NOT EXISTS "${MINHOOK_DIR}/include/MinHook.h")
        message(FATAL_ERROR "MinHook not found at ${MINHOOK_DIR}. Run 'git submodule update --init'")
    endif()
    # Built from source here rather than via its CMake project, whose install
    # rules would copy MinHook into the SteamVR driver folder.
    add_library(minhook STATIC
        "${MINHOOK_DIR}/src/buffer.c"
        "${MINHOOK_DIR}/src/hook.c"
        "${MINHOOK_DIR}/src/trampoline.c"
        "${MINHOOK_DIR}/src/hde/hde64.c"
    )
    target_include_directories(minhook PUBLIC "${MINHOOK_DIR}/include" PRIVATE "${MINHOOK_DIR}/src")
endif()

# ── Driver ──────────────────────────────────────────────────────────────────
set(SOURCES
    src/driver_main.cpp
    src/ServerProvider.cpp
    src/CyberFingerController.cpp
    src/StudioLink.cpp
    src/OpticalTap.cpp
    src/SkeletonSynth.cpp
    src/Utils.cpp
)

set(HEADERS
    src/ServerProvider.h
    src/CyberFingerController.h
    src/StudioLink.h
    src/OpticalTap.h
    src/SkeletonSynth.h
    src/NativeInput.h
    src/Protocol.h
    src/MathUtil.h
    src/BoneData.h
    src/Utils.h
    src/VrTypes.h
)

add_library(driver_cyberfinger SHARED ${SOURCES} ${HEADERS})

target_include_directories(driver_cyberfinger PRIVATE "${OPENVR_SDK}/headers" src)

set(DRIVER_DIR "${CMAKE_BINARY_DIR}/driver/cyberfinger")

if(WIN32)
    target_link_libraries(driver_cyberfinger PRIVATE minhook ws2_32)
    target_compile_definitions(driver_cyberfinger PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    foreach(cfg "" _DEBUG _RELEASE _RELWITHDEBINFO _MINSIZEREL)
        set_target_properties(driver_cyberfinger PROPERTIES
            RUNTIME_OUTPUT_DIRECTORY${cfg} "${DRIVER_DIR}/bin/win64"
            LIBRARY_OUTPUT_DIRECTORY${cfg} "${DRIVER_DIR}/bin/win64")
    endforeach()
elseif(UNIX)
    target_link_libraries(driver_cyberfinger PRIVATE pthread)
    set_target_properties(driver_cyberfinger PROPERTIES
        LIBRARY_OUTPUT_DIRECTORY "${DRIVER_DIR}/bin/linux64")
endif()

# ── Post-build: assemble build/driver/cyberfinger/ ─────────────────────────
add_custom_command(TARGET driver_cyberfinger POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy
        "${CMAKE_SOURCE_DIR}/resources/driver.vrdrivermanifest" "${DRIVER_DIR}/driver.vrdrivermanifest"
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${CMAKE_SOURCE_DIR}/resources/settings" "${DRIVER_DIR}/resources/settings"
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${CMAKE_SOURCE_DIR}/resources/input" "${DRIVER_DIR}/resources/input"
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${CMAKE_SOURCE_DIR}/resources/icons" "${DRIVER_DIR}/resources/icons"
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${CMAKE_SOURCE_DIR}/resources/rendermodels" "${DRIVER_DIR}/resources/rendermodels"
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${CMAKE_SOURCE_DIR}/resources/localization" "${DRIVER_DIR}/resources/localization"
    COMMENT "Assembling driver folder ${DRIVER_DIR}"
)

# ── Tests (no SteamVR needed) ──────────────────────────────────────────────
enable_testing()
add_executable(driver_tests tests/driver_tests.cpp src/SkeletonSynth.cpp)
target_include_directories(driver_tests PRIVATE "${OPENVR_SDK}/headers" src)
add_test(NAME driver_tests COMMAND driver_tests "${CMAKE_SOURCE_DIR}/tests/protocol_vectors.bin")

# Native game-mod telemetry: no SteamVR runtime or live shared mappings needed.
if(WIN32)
    add_executable(native_input_tests tests/native_input_tests.cpp)
    target_include_directories(native_input_tests PRIVATE "${OPENVR_SDK}/headers" src)
    target_compile_definitions(native_input_tests PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    add_test(NAME native_input_tests COMMAND native_input_tests "${CMAKE_CURRENT_BINARY_DIR}/native-frame.bin")
endif()

# Runs the passthrough pose filter over a recorded capture (tools/analyze_tap_capture.py --capture N).
add_executable(filter_eval tests/filter_eval.cpp)
target_include_directories(filter_eval PRIVATE "${OPENVR_SDK}/headers" src)

# Replays a capture with the CyberFinger IMUs through the driver-side IMU fusion (ImuFusion.h).
add_executable(fusion_eval tests/fusion_eval.cpp)
target_include_directories(fusion_eval PRIVATE "${OPENVR_SDK}/headers" src)

# ── Install: deploy to SteamVR drivers directory ──────────────────────────
# cmake --install . --prefix "C:/Program Files (x86)/Steam/steamapps/common/SteamVR/drivers"
# (or register build/driver/cyberfinger with vrpathreg adddriver)
set(DRIVER_INSTALL_NAME "cyberfinger" CACHE STRING "Driver folder name in SteamVR/drivers")

install(TARGETS driver_cyberfinger
    RUNTIME DESTINATION ${DRIVER_INSTALL_NAME}/bin/win64
    LIBRARY DESTINATION ${DRIVER_INSTALL_NAME}/bin/linux64
)
install(FILES resources/driver.vrdrivermanifest DESTINATION ${DRIVER_INSTALL_NAME})
foreach(dir settings input icons rendermodels localization)
    install(DIRECTORY resources/${dir}/ DESTINATION ${DRIVER_INSTALL_NAME}/resources/${dir})
endforeach()
