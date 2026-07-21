[app]
title = TITAN Pro Client
package.name = titanproclient
package.domain = org.titan.pro
source.dir = .
source.main = main.py
source.include_exts = py,png,jpg,kv,atlas,json
version = 0.1

# python3 без версии → авто-подстройка под hostpython3 на раннере
# sqlite3 → обязательно для работы OrderQueue на Android
requirements = python3,kivy,requests,urllib3,pytz,openssl,sqlite3,cython==3.0.10

android.permissions = INTERNET,WAKE_LOCK
android.api = 33
android.minapi = 21
android.ndk_api = 21
# android.ndk не указан → используем системный NDK (r27) на раннере — это стабильнее
android.accept_sdk_license = True
android.allow_insecure_keystore = True
android.archs = armeabi-v7a, arm64-v8a
orientation = portrait
fullscreen = 0
log_level = 2
warn_on_root = 0
android.gradle_options = -Xmx2048m

[buildozer]
log_level = 2
warn_on_root = 0
