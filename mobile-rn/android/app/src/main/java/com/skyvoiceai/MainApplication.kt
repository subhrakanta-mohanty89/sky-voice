package com.skyvoiceai

import android.app.Application
import com.facebook.react.ReactApplication
import com.facebook.react.ReactNativeHost
import com.facebook.react.defaults.DefaultNewArchitectureEntryPoint.load
import com.facebook.soloader.SoLoader
import com.twiliovoicereactnative.VoiceApplicationProxy

class MainApplication : Application(), ReactApplication {

  // The Twilio Voice SDK supplies its own ReactNativeHost subclass so it can
  // bind the call service and audio engine. It still loads the full autolinked
  // PackageList (see MainReactNativeHost).
  private val mReactNativeHost = MainReactNativeHost(this)

  // Owns the SDK's audio engine, notification channels and bound VoiceService.
  // MUST be constructed before React initialises the native module, otherwise
  // TwilioVoiceReactNativeModule's constructor dereferences a null
  // VoiceApplicationProxy singleton and the app crashes on launch.
  private val voiceApplicationProxy: VoiceApplicationProxy =
    VoiceApplicationProxy(mReactNativeHost)

  override val reactNativeHost: ReactNativeHost
    get() = mReactNativeHost

  override fun onCreate() {
    super.onCreate()
    voiceApplicationProxy.onCreate()
    SoLoader.init(this, false)
    if (BuildConfig.IS_NEW_ARCHITECTURE_ENABLED) {
      // If you opted-in for the New Architecture, we load the native entry point for this app.
      load()
    }
  }

  override fun onTerminate() {
    voiceApplicationProxy.onTerminate()
    super.onTerminate()
  }
}
