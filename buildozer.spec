[app]
title = TITAN Pro Client
package.name = titanproclient
package.domain = org.titan.pro
source.dir = .
source.main = main.py
source.include_exts = py,png,jpg,kv,atlas,json
version = 0.1

requirements = python3,kivy,aiohttp,pytz,sqlite3,openssl,cython>=3.0

android.python_version = 3.11

android.permissions = INTERNET,WAKE_LOCK
android.api = 33
android.minapi = 21
android.ndk_api = 21
android.ndk = 25b
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
