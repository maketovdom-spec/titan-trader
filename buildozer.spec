- name: Build APK with Buildozer
  timeout-minutes: 45
  run: |
    set +e
    set +o pipefail
    
    echo "🚀 Запуск первой попытки сборки..."
    # Отправляем 5 "y" для принятия всех лицензий, затем пайп закрывается. 
    # Теперь никакой Broken pipe!
    for i in {1..5}; do echo y; done | buildozer -v android debug
    FIRST_BUILD_STATUS=$?
    
    set -e
    set -o pipefail
    
    if [ $FIRST_BUILD_STATUS -ne 0 ]; then
      echo "⚠️ Первая попытка упала (Exit code: $FIRST_BUILD_STATUS). Вычищаем кэш и пробуем снова..."
      
      rm -rf bin/*
      rm -rf .buildozer/
      rm -rf ~/.buildozer/android/platform/
      
      echo "🔄 Запуск повторной чистой сборки..."
      for i in {1..5}; do echo y; done | buildozer -v android debug
    fi
  env:
    GRADLE_OPTS: "-Xmx4g -Dorg.gradle.daemon=false"
