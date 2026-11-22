[app]
title = GoldBull
package.name = rynok
package.domain = org.rynok
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
version = 0.2
icon.filename = %(source.dir)s/icon.png
requirements = python3==3.11.5,hostpython3==3.11.5,kivy==2.3.0,pyjnius,android,openssl,certifi
orientation = portrait
fullscreen = 0
android.archs = arm64-v8a
android.allow_backup = True
android.accept_sdk_license = True
android.api = 33
android.minapi = 21
android.ndk = 25b
android.permissions = INTERNET
android.extra_manifest_application_arguments = extra_manifest_application_arguments.xml
p4a.branch = v2024.01.21

[buildozer]
log_level = 2
warn_on_root = 1
