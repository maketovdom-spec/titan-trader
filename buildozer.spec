[app]
title = TITAN Pro Trader
package.name = titanproclient
package.domain = org.titan
source.dir = .
source.include_exts = py,png,jpg,kv,json,txt
version = 1.0.0

# 🛠 ФИНАЛЬНАЯ СТРОКА: Python 3.11, aiohttp 3.9.0, Cython 3.0.11
requirements = python3,kivy,aiohttp==3.9.0,pytz,pyjnius,sqlite3,openssl,cython==3.0.11

# 🛠 Явно говорим p4a использовать Python 3.11
android.python_version = 3.11

orientation = portrait
android.permissions = INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK
android.api = 31
android.minapi = 21
android.ndk = 25b
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
