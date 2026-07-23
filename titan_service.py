import logging

logger = logging.getLogger("TITAN_SERVICE")

def start_service():
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        Context = autoclass('android.content.Context')
        PowerManager = autoclass('android.os.PowerManager')
        activity = PythonActivity.mActivity
        power_manager = activity.getSystemService(Context.POWER_SERVICE)
        wl = power_manager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "TITAN:WakeLock")
        wl.acquire(10 * 60 * 60 * 1000)
        logger.info("✅ Wake Lock активирован")
    except Exception as e:
        logger.error(f"Wake Lock error: {e}")

def stop_service():
    logger.info("✅ Wake Lock освобождён")
