//
//  RGCamera.h
//  AIMoYan
//
//  Camera capture: front/back, 720p/1080p, portrait/landscape, tap-to-focus, pinch zoom.
//  Frames are delivered upright and NOT mirrored (the server sees the real scene);
//  the on-screen preview of the front camera is mirrored like a selfie.
//

#import <AVFoundation/AVFoundation.h>

NS_ASSUME_NONNULL_BEGIN

typedef NS_ENUM(NSInteger, RGQuality) {
    RGQuality720p = 0,
    RGQuality1080p = 1,
};

@interface RGCamera : NSObject
@property (nonatomic, strong, readonly) AVCaptureSession *session;
@property (nonatomic, readonly) AVCaptureDevicePosition position;
@property (nonatomic, readonly) RGQuality quality;
@property (nonatomic, readonly) BOOL portrait;
/// The on-screen preview; its rotation is kept correct for whichever camera is active.
@property (nonatomic, weak, nullable) AVCaptureVideoPreviewLayer *previewLayer;
/// Called on the capture queue with each upright camera frame.
@property (nonatomic, copy, nullable) void (^onFrame)(CVPixelBufferRef pixelBuffer);

- (void)startWithPosition:(AVCaptureDevicePosition)position quality:(RGQuality)quality portrait:(BOOL)portrait
               completion:(void (^)(NSError *_Nullable error))completion;
- (void)stop;
/// Restarts the session after -stop (e.g. after the QR scanner closed), keeping camera / quality.
- (void)resume;
- (void)switchCamera;
- (void)setQuality:(RGQuality)quality portrait:(BOOL)portrait;
/// devicePoint: (0,0)…(1,1) from AVCaptureVideoPreviewLayer -captureDevicePointOfInterestForPoint:
- (void)focusAtDevicePoint:(CGPoint)devicePoint;
/// Focus + meter at a point, then lock both (long-press). Undone by the next -focusAtDevicePoint:.
- (void)lockFocusAndExposureAtDevicePoint:(CGPoint)devicePoint;
@property (nonatomic, readonly) BOOL focusExposureLocked;
/// Brightness (exposure compensation) in EV, clamped to ±2 and to what the camera supports.
- (float)setExposureBias:(float)bias;
/// Multiplies the current zoom (pinch scale), clamped to what the camera supports.
- (CGFloat)zoomBy:(CGFloat)scale fromZoom:(CGFloat)startZoom;
- (CGFloat)currentZoom;

/// Output frame size for a quality / orientation.
+ (CGSize)frameSizeForQuality:(RGQuality)quality portrait:(BOOL)portrait;
@end

NS_ASSUME_NONNULL_END
