package com.skyvoiceai

import android.Manifest
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import com.facebook.react.ReactActivity
import com.facebook.react.ReactActivityDelegate
import com.facebook.react.defaults.DefaultNewArchitectureEntryPoint.fabricEnabled
import com.facebook.react.defaults.DefaultReactActivityDelegate
import com.twiliovoicereactnative.VoiceActivityProxy

class MainActivity : ReactActivity() {

  /**
   * The Twilio Voice SDK proxy handles runtime permission requests
   * (microphone / bluetooth / notifications) and routes incoming-call intents
   * into the SDK's foreground call service.
   */
  private val activityProxy = VoiceActivityProxy(this) { permission ->
    when {
      Manifest.permission.RECORD_AUDIO == permission ->
        Toast.makeText(
          this,
          "Microphone permission is needed to make calls. Please allow it in Settings.",
          Toast.LENGTH_LONG,
        ).show()

      Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
        Manifest.permission.BLUETOOTH_CONNECT == permission ->
        Toast.makeText(
          this,
          "Bluetooth permission is needed for headset calls. Please allow it in Settings.",
          Toast.LENGTH_LONG,
        ).show()

      Build.VERSION.SDK_INT > Build.VERSION_CODES.S_V2 &&
        Manifest.permission.POST_NOTIFICATIONS == permission ->
        Toast.makeText(
          this,
          "Notification permission is needed for call alerts. Please allow it in Settings.",
          Toast.LENGTH_LONG,
        ).show()
    }
  }

  /**
   * Returns the name of the main component registered from JavaScript. This is used to schedule
   * rendering of the component.
   */
  override fun getMainComponentName(): String = "SkyVoiceAI"

  /**
   * react-native-screens requires passing `null` to super.onCreate so Android
   * does not try to restore fragment state into a not-yet-ready React view
   * hierarchy (which otherwise crashes on activity recreation / rotation).
   * The Twilio proxy ignores the saved instance state, so this is safe.
   */
  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(null)
    activityProxy.onCreate(savedInstanceState)
  }

  override fun onDestroy() {
    activityProxy.onDestroy()
    super.onDestroy()
  }

  override fun onNewIntent(intent: Intent) {
    activityProxy.onNewIntent(intent)
    super.onNewIntent(intent)
  }

  /**
   * Returns the instance of the [ReactActivityDelegate]. We use [DefaultReactActivityDelegate]
   * which allows you to enable New Architecture with a single boolean flags [fabricEnabled]
   */
  override fun createReactActivityDelegate(): ReactActivityDelegate =
      DefaultReactActivityDelegate(this, mainComponentName, fabricEnabled)
}
