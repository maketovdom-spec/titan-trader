[app]
title = TITAN Pro Trader
package.name = titanproclient
package.domain = org.titan
source.dir = .
source.include_exts = py,png,jpg,kv,json,txt
version = 0.1.0

# ИСПРАВЛЕНО: Добавлены критически важные pyjnius, sqlite3 и openssl
requirements = python3,kivy,aiohttp,pytz,pyjnius,sqlite3,openssl,cython<3.0

orientation = portrait

android.permissions = INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK

android.api = 31
android.minapi = 21
android.ndk = 25b
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
