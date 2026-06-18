package com.skyvoiceai

import android.app.Application
import com.facebook.react.PackageList
import com.facebook.react.ReactPackage
import com.twiliovoicereactnative.VoiceApplicationProxy

/**
 * The Twilio Voice React Native SDK requires the app's `ReactNativeHost` to be
 * a `VoiceApplicationProxy.VoiceReactNativeHost` so the SDK can hook into the
 * application lifecycle (audio engine, notification channels, foreground call
 * service). We still return the full autolinked `PackageList`, which already
 * includes the Twilio package plus every other native module.
 */
class MainReactNativeHost(application: Application) :
  VoiceApplicationProxy.VoiceReactNativeHost(application) {

  override fun getUseDeveloperSupport(): Boolean = BuildConfig.DEBUG

  override fun getPackages(): List<ReactPackage> = PackageList(this).packages

  override fun getJSMainModuleName(): String = "index"
}
