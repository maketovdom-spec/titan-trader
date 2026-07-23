[app]
title = TITAN Pro Client
package.name = titanproclient
package.domain = org.titan.pro
source.dir = .
source.main = main.py
source.include_exts = py,png,jpg,kv,atlas,json,so,db
version = 0.1

# ВСЕ зависимости для многозадачности: aiohttp, sqlite3, certifi, cryptography
requirements = python3,kivy==2.3.0,aiohttp,requests,urllib3,pytz,sqlite3,certifi,cryptography,cython==3.0.10

android.permissions = INTERNET,WAKE_LOCK
android.api = 33
android.minapi = 21
android.ndk_api = 21
# NDK не фиксируем - пусть берёт системный
android.accept_sdk_license = True
android.allow_insecure_keystore = True
android.archs = arm64-v8a

orientation = portrait
fullscreen = 0
log_level = 2
warn_on_root = 0
android.gradle_options = -Xmx2048m

[buildozer]
log_level = 2
warn_on_root = 0
