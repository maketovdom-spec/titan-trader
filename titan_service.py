import logging

logger = logging.getLogger("TITAN_SERVICE")
SERVICE_RUNNING = False

def start_service():
    global SERVICE_RUNNING
    if SERVICE_RUNNING:
        return True
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        Context = autoclass('android.content.Context')
        PowerManager = autoclass('android.os.PowerManager')
        
        activity = PythonActivity.mActivity
        power_manager = activity.getSystemService(Context.POWER_SERVICE)
        
        wake_lock = power_manager.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK, 
            "TITAN::WakeLock"
        )
        wake_lock.acquire(10 * 60 * 60 * 1000)
        
        SERVICE_RUNNING = True
        logger.info("✅ Wake Lock активирован")
        return True
    except Exception as e:
        logger.error(f"⚠️ Ошибка Wake Lock: {e}")
        return False

def stop_service():
    global SERVICE_RUNNING
    if not SERVICE_RUNNING:
        return True
    try:
        SERVICE_RUNNING = False
        logger.info("✅ Wake Lock освобождён")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка остановки: {e}")
        return False
