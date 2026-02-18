cmake_minimum_required(VERSION 3.16)
project(driver_cyberfinger VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# ── OpenVR SDK ──────────────────────────────────────────────────────────────
# Set OPENVR_SDK to the root of your openvr checkout
if(NOT DEFINED OPENVR_SDK)
    set(OPENVR_SDK "${CMAKE_SOURCE_DIR}/openvr" CACHE PATH "Path to OpenVR SDK")
endif()

if(NOT EXISTS "${OPENVR_SDK}/headers/openvr_driver.h")
    message(FATAL_ERROR
        "OpenVR SDK not found at ${OPENVR_SDK}. "
        "Clone https://github.com/ValveSoftware/openvr into ${CMAKE_SOURCE_DIR}/openvr "
        "or set -DOPENVR_SDK=<path>")
endif()

# ── Sources ─────────────────────────────────────────────────────────────────
set(SOURCES
    src/driver_main.cpp
    src/ServerProvider.cpp
    src/MergedController.cpp
    src/HandTrackingReceiver.cpp
    src/SkeletonComposer.cpp
    src/Utils.cpp
)

set(HEADERS
    src/ServerProvider.h
    src/MergedController.h
    src/HandTrackingReceiver.h
    src/SkeletonComposer.h
    src/Utils.h
    src/BoneData.h
)

add_library(driver_cyberfinger SHARED ${SOURCES} ${HEADERS})

target_include_directories(driver_cyberfinger PRIVATE
    "${OPENVR_SDK}/headers"
    src
)

# ── Platform specifics ──────────────────────────────────────────────────────
if(WIN32)
    target_link_libraries(driver_cyberfinger PRIVATE
        "${OPENVR_SDK}/lib/win64/openvr_api.lib"
        ws2_32
    )
    set_target_properties(driver_cyberfinger PROPERTIES
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/driver/cyberfinger/bin/win64"
        LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/driver/cyberfinger/bin/win64"
        RUNTIME_OUTPUT_DIRECTORY_DEBUG "${CMAKE_BINARY_DIR}/driver/cyberfinger/bin/win64"
        RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/driver/cyberfinger/bin/win64"
        LIBRARY_OUTPUT_DIRECTORY_DEBUG "${CMAKE_BINARY_DIR}/driver/cyberfinger/bin/win64"
        LIBRARY_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/driver/cyberfinger/bin/win64"
    )
elseif(UNIX)
    find_library(OPENVR_LIB openvr_api PATHS "${OPENVR_SDK}/lib/linux64")
    target_link_libraries(driver_cyberfinger PRIVATE
        ${OPENVR_LIB}
        pthread
    )
    set_target_properties(driver_cyberfinger PROPERTIES
        LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/driver/cyberfinger/bin/linux64"
    )
endif()

# ── Hand Tracking Bridge executable ─────────────────────────────────────────
add_executable(handtracking_bridge bridge/handtracking_bridge.cpp)

target_include_directories(handtracking_bridge PRIVATE
    "${OPENVR_SDK}/headers"
)

if(WIN32)
    target_link_libraries(handtracking_bridge PRIVATE
        "${OPENVR_SDK}/lib/win64/openvr_api.lib"
        ws2_32
    )
else()
    target_link_libraries(handtracking_bridge PRIVATE
        ${OPENVR_LIB}
        pthread
    )
endif()

# Copy openvr_api.dll next to the bridge exe so it can find it
if(WIN32 AND EXISTS "${OPENVR_SDK}/bin/win64/openvr_api.dll")
    add_custom_command(TARGET handtracking_bridge POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy
            "${OPENVR_SDK}/bin/win64/openvr_api.dll"
            "$<TARGET_FILE_DIR:handtracking_bridge>/openvr_api.dll"
        COMMENT "Copying openvr_api.dll next to bridge executable"
    )
endif()

# ── Post-build: copy resources into build/driver/cyberfinger/ ─────────
set(DRIVER_DIR "${CMAKE_BINARY_DIR}/driver/cyberfinger")

add_custom_command(TARGET driver_cyberfinger POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy
        "${CMAKE_SOURCE_DIR}/resources/driver.vrdrivermanifest"
        "${DRIVER_DIR}/driver.vrdrivermanifest"
    COMMAND ${CMAKE_COMMAND} -E make_directory
        "${DRIVER_DIR}/resources/settings"
    COMMAND ${CMAKE_COMMAND} -E copy
        "${CMAKE_SOURCE_DIR}/resources/settings/default.vrsettings"
        "${DRIVER_DIR}/resources/settings/default.vrsettings"
    COMMAND ${CMAKE_COMMAND} -E copy
        "${CMAKE_SOURCE_DIR}/resources/settings/settingsschema.vrsettings"
        "${DRIVER_DIR}/resources/settings/settingsschema.vrsettings"
    COMMAND ${CMAKE_COMMAND} -E make_directory
        "${DRIVER_DIR}/resources/input"
    COMMAND ${CMAKE_COMMAND} -E copy
        "${CMAKE_SOURCE_DIR}/resources/input/cyberfinger_profile.json"
        "${DRIVER_DIR}/resources/input/cyberfinger_profile.json"
    COMMAND ${CMAKE_COMMAND} -E copy
        "${CMAKE_SOURCE_DIR}/resources/input/cyberfinger_bindings.json"
        "${DRIVER_DIR}/resources/input/cyberfinger_bindings.json"
    COMMAND ${CMAKE_COMMAND} -E make_directory
        "${DRIVER_DIR}/resources/icons"
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${CMAKE_SOURCE_DIR}/resources/icons"
        "${DRIVER_DIR}/resources/icons"
    COMMENT "Copying driver resources to ${DRIVER_DIR}"
)

# ── Install: deploy to SteamVR drivers directory ──────────────────────────
# Usage:  cmake --install . --prefix "C:/Program Files (x86)/Steam/steamapps/common/SteamVR/drivers"
# Or set CMAKE_INSTALL_PREFIX in CMake settings / CMakePresets.json

set(DRIVER_INSTALL_NAME "cyberfinger" CACHE STRING "Driver folder name in SteamVR/drivers")

install(TARGETS driver_cyberfinger
    RUNTIME DESTINATION ${DRIVER_INSTALL_NAME}/bin/win64
    LIBRARY DESTINATION ${DRIVER_INSTALL_NAME}/bin/linux64
)

install(FILES
    resources/driver.vrdrivermanifest
    DESTINATION ${DRIVER_INSTALL_NAME}
)

install(DIRECTORY resources/settings/
    DESTINATION ${DRIVER_INSTALL_NAME}/resources/settings
)

install(DIRECTORY resources/input/
    DESTINATION ${DRIVER_INSTALL_NAME}/resources/input
)

install(DIRECTORY resources/icons/
    DESTINATION ${DRIVER_INSTALL_NAME}/resources/icons
)
