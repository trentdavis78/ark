package com.blacktop.capture

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.Looper
import android.util.DisplayMetrics
import androidx.core.graphics.createBitmap
import kotlinx.coroutines.CoroutineScope

/**
 * MediaProjection screen capture, used only to feed [VisionFallback].
 *
 * The driver grants this explicitly through the system consent dialog, and can
 * revoke it at any time; without the grant the app runs on node capture alone.
 * Frames are read, handed to the vision endpoint, and dropped — nothing is
 * written to disk.
 */
class ScreenCapture(
    private val context: Context,
    val scope: CoroutineScope,
) {
    private var projection: MediaProjection? = null
    private var reader: ImageReader? = null
    private var display: VirtualDisplay? = null
    private val handler = Handler(Looper.getMainLooper())

    val isGranted: Boolean get() = projection != null

    fun consentIntent(): Intent =
        (context.getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager)
            .createScreenCaptureIntent()

    fun onConsentResult(resultCode: Int, data: Intent?) {
        if (resultCode != Activity.RESULT_OK || data == null) return
        val manager =
            context.getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        projection = manager.getMediaProjection(resultCode, data)?.also { mp ->
            mp.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() = release()
            }, handler)
        }
    }

    /** Grab a single frame. Calls back with null when capture is unavailable. */
    fun requestFrame(onFrame: (Bitmap?) -> Unit) {
        val mp = projection ?: return onFrame(null)
        val metrics = DisplayMetrics().also {
            @Suppress("DEPRECATION")
            context.resources.displayMetrics.let { dm ->
                it.widthPixels = dm.widthPixels
                it.heightPixels = dm.heightPixels
                it.densityDpi = dm.densityDpi
            }
        }
        val ir = ImageReader.newInstance(
            metrics.widthPixels, metrics.heightPixels, PixelFormat.RGBA_8888, 2)
        reader = ir
        display = mp.createVirtualDisplay(
            "blacktop-capture",
            metrics.widthPixels, metrics.heightPixels, metrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            ir.surface, null, handler,
        )

        ir.setOnImageAvailableListener({ r ->
            val image = r.acquireLatestImage() ?: return@setOnImageAvailableListener
            val bitmap = try {
                val plane = image.planes[0]
                val rowPadding = plane.rowStride - plane.pixelStride * metrics.widthPixels
                createBitmap(
                    metrics.widthPixels + rowPadding / plane.pixelStride,
                    metrics.heightPixels,
                ).apply { copyPixelsFromBuffer(plane.buffer) }
            } catch (_: Exception) {
                null
            } finally {
                image.close()
            }
            release()
            onFrame(bitmap)
        }, handler)
    }

    fun release() {
        display?.release()
        display = null
        reader?.close()
        reader = null
    }

    fun stop() {
        release()
        projection?.stop()
        projection = null
    }
}
