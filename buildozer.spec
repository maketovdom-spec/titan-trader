[app]
title = TITAN Pro Client
package.name = titanproclient
package.domain = org.titan.pro
source.dir = .
source.main = main.py
source.include_exts = py,png,jpg,kv,atlas,json
version = 0.1

# ВАЖНО: python3==3.11.1 — фиксируем версию, чтобы сборка и запуск были предсказуемыми.
# kivy==2.3.0 — стабильная ветка под Cython 3.x.
# sqlite3 — рецепт для Android (OrderQueue).
requirements = python3==3.11.1,kivy==2.3.0,requests,urllib3,pytz,openssl,sqlite3,cython==3.0.10

android.permissions = INTERNET,WAKE_LOCK
android.api = 33
android.minapi = 21
android.ndk_api = 21

# android.ndk = 25b — стабильная версия NDK для API 33 и p4a
android.ndk = 25b

android.accept_sdk_license = True
android.allow_insecure_keystore = True

# Для теста на телефоне можно оставить только arm64-v8a, для релиза — обе
android.archs = armeabi-v7a, arm64-v8a

orientation = portrait
fullscreen = 0
log_level = 2
warn_on_root = 0
android.gradle_options = -Xmx2048m

[buildozer]
log_level = 2
warn_on_root = 0
