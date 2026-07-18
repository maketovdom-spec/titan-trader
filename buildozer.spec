[app]
title = TITAN Pro Trader
package.name = titanproclient
package.domain = org.titan
source.dir = .
source.include_exts = py,png,jpg,kv,json,txt
version = 1.0.0

# ПОЛНЫЙ набор для торговой системы
requirements = python3,kivy,aiohttp,pytz,pyjnius,sqlite3,openssl,certifi,async_timeout,cython<3.0

orientation = portrait

android.permissions = INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK

android.api = 31
android.minapi = 21
android.ndk = 25b
android.accept_sdk_license = True
android.archs = arm64-v8a

[buildozer]
log_level = 2
warn_on_root = 1
