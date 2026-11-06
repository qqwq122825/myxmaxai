//
//  RGCamera.m
//  AIMoYan
//

#import "RGCamera.h"
#include <math.h>

@interface RGCamera () <AVCaptureVideoDataOutputSampleBufferDelegate, AVCaptureAudioDataOutputSampleBufferDelegate>
@property (nonatomic, strong, readwrite) AVCaptureSession *session;
@property (nonatomic, readwrite) AVCaptureDevicePosition position;
@property (nonatomic, readwrite) RGQuality quality;
@property (nonatomic, readwrite) BOOL portrait;
@property (nonatomic, strong) dispatch_queue_t sessionQueue;
@property (nonatomic, strong) dispatch_queue_t videoQueue;
@property (nonatomic, strong) dispatch_queue_t audioQueue;
@property (nonatomic, strong, nullable) AVCaptureDeviceInput *microphone;
@property (nonatomic, strong, nullable) AVCaptureAudioDataOutput *audioOutput;
// Used only on audioQueue.
@property (nonatomic, copy, nullable) void (^onPCM)(NSData *);
@property (nonatomic, strong, nullable) AVAudioConverter *pcmConverter;
@property (nonatomic, strong, nullable) AVCaptureDeviceInput *input;
@property (nonatomic, strong) AVCaptureVideoDataOutput *output;
@property (nonatomic, strong, nullable) id rotationCoordinator;   // AVCaptureDeviceRotationCoordinator (iOS 17+)
@property (nonatomic, readwrite) BOOL focusExposureLocked;
@end

@implementation RGCamera

+ (CGSize)frameSizeForQuality:(RGQuality)quality portrait:(BOOL)portrait {
    CGSize size = quality == RGQuality1080p ? CGSizeMake(1920, 1080) : CGSizeMake(1280, 720);
    return portrait ? CGSizeMake(size.height, size.width) : size;
}

- (instancetype)init {
    if ((self = [super init])) {
        _session = [AVCaptureSession new];
        _sessionQueue = dispatch_queue_create("AIMoYan.camera.session", DISPATCH_QUEUE_SERIAL);
        _videoQueue = dispatch_queue_create("AIMoYan.camera.video", DISPATCH_QUEUE_SERIAL);
        _audioQueue = dispatch_queue_create("AIMoYan.camera.audio", DISPATCH_QUEUE_SERIAL);
        _output = [AVCaptureVideoDataOutput new];
        _output.alwaysDiscardsLateVideoFrames = YES;
        _output.videoSettings = @{(id)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_420YpCbCr8BiPlanarFullRange)};
        [_output setSampleBufferDelegate:self queue:_videoQueue];
        _position = AVCaptureDevicePositionBack;
        _portrait = YES;

        [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(subjectAreaDidChange:)
                                                   name:AVCaptureDeviceSubjectAreaDidChangeNotification object:nil];
    }
    return self;
}

- (void)dealloc {
    [NSNotificationCenter.defaultCenter removeObserver:self];
}

- (AVCaptureSessionPreset)presetForQuality:(RGQuality)quality {
    return quality == RGQuality1080p ? AVCaptureSessionPreset1920x1080 : AVCaptureSessionPreset1280x720;
}

- (void)startWithPosition:(AVCaptureDevicePosition)position quality:(RGQuality)quality portrait:(BOOL)portrait
               completion:(void (^)(NSError *_Nullable))completion {
    dispatch_async(self.sessionQueue, ^{
        self.position = position; self.quality = quality; self.portrait = portrait;
        NSError *error = nil;
        [self.session beginConfiguration];
        if ([self.session canAddOutput:self.output]) [self.session addOutput:self.output];
        [self applyInputForPosition:position error:&error];
        [self applyQualityLocked];
        [self.session commitConfiguration];
        [self updateRotationLocked];   // connections for a new camera exist only after commit
        if (!error) [self.session startRunning];
        dispatch_async(dispatch_get_main_queue(), ^{ completion(error); });
    });
}

- (void)stop {
    dispatch_async(self.sessionQueue, ^{ if (self.session.isRunning) [self.session stopRunning]; });
}

- (void)startMicrophone:(void (^)(NSData *))handler completion:(void (^)(BOOL))completion {
    dispatch_async(self.sessionQueue, ^{
        if (self.audioOutput) {                    // already running (quick off → on): keep it, new handler
            dispatch_sync(self.audioQueue, ^{ self.onPCM = handler; });
            dispatch_async(dispatch_get_main_queue(), ^{ completion(YES); });
            return;
        }
        AVCaptureDevice *device = [AVCaptureDevice defaultDeviceWithMediaType:AVMediaTypeAudio];
        AVCaptureDeviceInput *input = device ? [AVCaptureDeviceInput deviceInputWithDevice:device error:nil] : nil;
        AVCaptureAudioDataOutput *output = [AVCaptureAudioDataOutput new];
        BOOL started = NO;
        [self.session beginConfiguration];
        if (!self.audioOutput && input && [self.session canAddInput:input]) {
            [self.session addInput:input];
            if ([self.session canAddOutput:output]) {
                dispatch_sync(self.audioQueue, ^{ self.onPCM = handler; });
                [output setSampleBufferDelegate:self queue:self.audioQueue];
                [self.session addOutput:output];
                self.microphone = input;
                self.audioOutput = output;
                started = YES;
            } else {
                [self.session removeInput:input];
            }
        }
        [self.session commitConfiguration];
        dispatch_async(dispatch_get_main_queue(), ^{ completion(started); });
    });
}

- (void)stopMicrophone {
    dispatch_async(self.sessionQueue, ^{
        if (!self.audioOutput) return;
        [self.audioOutput setSampleBufferDelegate:nil queue:NULL];
        dispatch_sync(self.audioQueue, ^{
            self.onPCM = nil;
            self.pcmConverter = nil;
        });
        [self.session beginConfiguration];
        [self.session removeOutput:self.audioOutput];
        [self.session removeInput:self.microphone];
        self.audioOutput = nil;
        self.microphone = nil;
        [self.session commitConfiguration];
    });
}

- (void)resume {
    dispatch_async(self.sessionQueue, ^{
        if (self.input && !self.session.isRunning) [self.session startRunning];
    });
}

- (void)switchCamera {
    dispatch_async(self.sessionQueue, ^{
        AVCaptureDevicePosition next = self.position == AVCaptureDevicePositionBack ? AVCaptureDevicePositionFront
                                                                                     : AVCaptureDevicePositionBack;
        self.focusExposureLocked = NO;
        [self.session beginConfiguration];
        NSError *error = nil;
        if ([self applyInputForPosition:next error:&error]) self.position = next;
        [self applyQualityLocked];
        [self.session commitConfiguration];
        [self updateRotationLocked];   // connections for a new camera exist only after commit
    });
}

- (void)setQuality:(RGQuality)quality portrait:(BOOL)portrait {
    dispatch_async(self.sessionQueue, ^{
        self.quality = quality; self.portrait = portrait;
        [self.session beginConfiguration];
        [self applyQualityLocked];
        [self.session commitConfiguration];
        [self updateRotationLocked];   // connections for a new camera exist only after commit
    });
}

// Must be called between begin/commitConfiguration on the session queue.
- (BOOL)applyInputForPosition:(AVCaptureDevicePosition)position error:(NSError **)error {
    AVCaptureDevice *device = [AVCaptureDevice defaultDeviceWithDeviceType:AVCaptureDeviceTypeBuiltInWideAngleCamera
                                                                mediaType:AVMediaTypeVideo position:position];
    if (!device) {
        if (error) *error = [NSError errorWithDomain:@"AIMoYan.Camera" code:1
                                            userInfo:@{NSLocalizedDescriptionKey: @"找不到摄像头。"}];
        return NO;
    }
    AVCaptureDeviceInput *input = [AVCaptureDeviceInput deviceInputWithDevice:device error:error];
    if (!input) return NO;
    if (self.input) [self.session removeInput:self.input];
    if ([self.session canAddInput:input]) {
        [self.session addInput:input];
        self.input = input;
    } else if (self.input) {
        [self.session addInput:self.input];   // put the old one back
        return NO;
    }
    if ([device lockForConfiguration:nil]) {
        if ([device isFocusModeSupported:AVCaptureFocusModeContinuousAutoFocus]) device.focusMode = AVCaptureFocusModeContinuousAutoFocus;
        if ([device isExposureModeSupported:AVCaptureExposureModeContinuousAutoExposure]) device.exposureMode = AVCaptureExposureModeContinuousAutoExposure;
        device.subjectAreaChangeMonitoringEnabled = YES;
        [device unlockForConfiguration];
    }
    return YES;
}

- (void)applyQualityLocked {
    AVCaptureSessionPreset preset = [self presetForQuality:self.quality];
    if (![self.session canSetSessionPreset:preset]) preset = AVCaptureSessionPreset1280x720;
    self.session.sessionPreset = preset;

    AVCaptureDevice *device = self.input.device;
    if (device && [device lockForConfiguration:nil]) {
        CMTime thirtyFps = CMTimeMake(1, 30);
        for (AVFrameRateRange *range in device.activeFormat.videoSupportedFrameRateRanges) {
            if (range.maxFrameRate >= 30 && range.minFrameRate <= 30) {
                device.activeVideoMinFrameDuration = thirtyFps;
                device.activeVideoMaxFrameDuration = thirtyFps;
                break;
            }
        }
        [device unlockForConfiguration];
    }

    AVCaptureConnection *connection = [self.output connectionWithMediaType:AVMediaTypeVideo];
    if (connection.isVideoMirroringSupported) {
        connection.automaticallyAdjustsVideoMirroring = NO;
        connection.videoMirrored = NO;          // the server gets the true (un-mirrored) image
    }
    [self updateRotationLocked];
}

/// Front and back cameras are mounted at different angles (and differ between iPhone models),
/// so ask the system for the right angle per camera instead of assuming 90°.
- (void)updateRotationLocked {
    AVCaptureConnection *output = [self.output connectionWithMediaType:AVMediaTypeVideo];
    AVCaptureVideoPreviewLayer *previewLayer = self.previewLayer;
    if (@available(iOS 17.0, *)) {
        AVCaptureDevice *device = self.input.device;
        if (!device) return;
        AVCaptureDeviceRotationCoordinator *coordinator = self.rotationCoordinator;
        if (!coordinator || coordinator.device != device) {
            coordinator = [[AVCaptureDeviceRotationCoordinator alloc] initWithDevice:device previewLayer:previewLayer];
            self.rotationCoordinator = coordinator;
        }
        // The UI is locked to portrait, so the preview angle is the "upright portrait" angle for this camera.
        CGFloat uprightPortrait = coordinator.videoRotationAngleForHorizonLevelPreview;
        CGFloat captureAngle = self.portrait ? uprightPortrait : fmod(uprightPortrait + 270.0, 360.0);
        if (output && [output isVideoRotationAngleSupported:captureAngle]) output.videoRotationAngle = captureAngle;
        dispatch_async(dispatch_get_main_queue(), ^{
            AVCaptureConnection *preview = previewLayer.connection;
            if (preview && [preview isVideoRotationAngleSupported:uprightPortrait]) preview.videoRotationAngle = uprightPortrait;
        });
    } else {
        if (output.isVideoOrientationSupported) {
            output.videoOrientation = self.portrait ? AVCaptureVideoOrientationPortrait
                                                    : AVCaptureVideoOrientationLandscapeRight;
        }
        dispatch_async(dispatch_get_main_queue(), ^{
            AVCaptureConnection *preview = previewLayer.connection;
            if (preview.isVideoOrientationSupported) preview.videoOrientation = AVCaptureVideoOrientationPortrait;
        });
    }
}

#pragma mark Focus / zoom

- (void)focusAtDevicePoint:(CGPoint)point {
    dispatch_async(self.sessionQueue, ^{
        self.focusExposureLocked = NO;
        AVCaptureDevice *device = self.input.device;
        if (!device || ![device lockForConfiguration:nil]) return;
        [device setExposureTargetBias:0 completionHandler:nil];     // a new tap starts from normal brightness
        if (device.isFocusPointOfInterestSupported && [device isFocusModeSupported:AVCaptureFocusModeAutoFocus]) {
            device.focusPointOfInterest = point;
            device.focusMode = AVCaptureFocusModeAutoFocus;
        }
        if (device.isExposurePointOfInterestSupported && [device isExposureModeSupported:AVCaptureExposureModeAutoExpose]) {
            device.exposurePointOfInterest = point;
            device.exposureMode = AVCaptureExposureModeAutoExpose;
        }
        device.subjectAreaChangeMonitoringEnabled = YES;
        [device unlockForConfiguration];
    });
}

- (void)lockFocusAndExposureAtDevicePoint:(CGPoint)point {
    [self focusAtDevicePoint:point];
    dispatch_async(self.sessionQueue, ^{ self.focusExposureLocked = YES; });
    // Give focus/exposure a moment to settle on the new point, then freeze them.
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.6 * NSEC_PER_SEC)), self.sessionQueue, ^{
        AVCaptureDevice *device = self.input.device;
        if (!self.focusExposureLocked || !device || ![device lockForConfiguration:nil]) return;
        if ([device isFocusModeSupported:AVCaptureFocusModeLocked]) device.focusMode = AVCaptureFocusModeLocked;
        if ([device isExposureModeSupported:AVCaptureExposureModeLocked]) device.exposureMode = AVCaptureExposureModeLocked;
        [device unlockForConfiguration];
    });
}

- (float)setExposureBias:(float)bias {
    AVCaptureDevice *device = self.input.device;
    if (!device) return 0;
    float clamped = MAX(MAX(device.minExposureTargetBias, -2.0f), MIN(MIN(device.maxExposureTargetBias, 2.0f), bias));
    if ([device lockForConfiguration:nil]) {
        [device setExposureTargetBias:clamped completionHandler:nil];
        [device unlockForConfiguration];
    }
    return clamped;
}

/// After a tap-to-focus, go back to continuous focus once the scene changes (unless the user locked it).
- (void)subjectAreaDidChange:(NSNotification *)note {
    dispatch_async(self.sessionQueue, ^{
        if (self.focusExposureLocked) return;
        AVCaptureDevice *device = self.input.device;
        if (!device || note.object != device || ![device lockForConfiguration:nil]) return;
        if ([device isFocusModeSupported:AVCaptureFocusModeContinuousAutoFocus]) {
            device.focusPointOfInterest = CGPointMake(0.5, 0.5);
            device.focusMode = AVCaptureFocusModeContinuousAutoFocus;
        }
        if ([device isExposureModeSupported:AVCaptureExposureModeContinuousAutoExposure]) {
            device.exposurePointOfInterest = CGPointMake(0.5, 0.5);
            device.exposureMode = AVCaptureExposureModeContinuousAutoExposure;
        }
        [device unlockForConfiguration];
    });
}

- (CGFloat)currentZoom {
    return self.input.device.videoZoomFactor ?: 1;
}

- (CGFloat)zoomBy:(CGFloat)scale fromZoom:(CGFloat)startZoom {
    AVCaptureDevice *device = self.input.device;
    if (!device) return 1;
    CGFloat maxZoom = MIN(device.maxAvailableVideoZoomFactor, 8.0);
    CGFloat zoom = MAX(device.minAvailableVideoZoomFactor, MIN(maxZoom, startZoom * scale));
    if ([device lockForConfiguration:nil]) {
        device.videoZoomFactor = zoom;
        [device unlockForConfiguration];
    }
    return zoom;
}

#pragma mark Frames

- (void)sendMicrophoneSample:(CMSampleBufferRef)sample {
    void (^handler)(NSData *) = self.onPCM;
    if (!handler) return;
    CMFormatDescriptionRef description = CMSampleBufferGetFormatDescription(sample);
    if (!description) return;
    const AudioStreamBasicDescription *stream = CMAudioFormatDescriptionGetStreamBasicDescription(description);
    if (!stream || stream->mFormatID != kAudioFormatLinearPCM || stream->mSampleRate <= 0) return;
    AVAudioFormat *inputFormat = [[AVAudioFormat alloc] initWithStreamDescription:stream];
    CMItemCount count = CMSampleBufferGetNumSamples(sample);
    if (!inputFormat || count <= 0 || count > 16384) return;
    AVAudioPCMBuffer *input = [[AVAudioPCMBuffer alloc] initWithPCMFormat:inputFormat frameCapacity:(AVAudioFrameCount)count];
    if (!input) return;
    input.frameLength = (AVAudioFrameCount)count;
    if (CMSampleBufferCopyPCMDataIntoAudioBufferList(sample, 0, (int32_t)count, input.mutableAudioBufferList) != noErr) return;

    if (!self.pcmConverter || ![self.pcmConverter.inputFormat isEqual:inputFormat]) {
        AVAudioFormat *format = [[AVAudioFormat alloc] initWithCommonFormat:AVAudioPCMFormatInt16
                                                               sampleRate:48000 channels:1 interleaved:YES];
        self.pcmConverter = [[AVAudioConverter alloc] initFromFormat:inputFormat toFormat:format];
    }
    AVAudioConverter *converter = self.pcmConverter;
    if (!converter) return;
    AVAudioFrameCount capacity = (AVAudioFrameCount)ceil(count * 48000.0 / stream->mSampleRate) + 64;
    AVAudioPCMBuffer *output = [[AVAudioPCMBuffer alloc] initWithPCMFormat:converter.outputFormat frameCapacity:capacity];
    if (!output) return;
    __block BOOL supplied = NO;
    AVAudioConverterOutputStatus status = [converter convertToBuffer:output error:nil
        withInputFromBlock:^AVAudioBuffer *(AVAudioPacketCount requested, AVAudioConverterInputStatus *outStatus) {
        *outStatus = supplied ? AVAudioConverterInputStatus_NoDataNow : AVAudioConverterInputStatus_HaveData;
        if (supplied) return nil;
        supplied = YES;
        return input;
    }];
    if (status == AVAudioConverterOutputStatus_Error || !output.frameLength) return;
    const uint8_t *pcm = output.audioBufferList->mBuffers[0].mData;
    if (!pcm) return;
    NSUInteger length = output.frameLength * sizeof(int16_t);
    for (NSUInteger offset = 0; offset < length; offset += 1920) {
        handler([NSData dataWithBytes:pcm + offset length:MIN((NSUInteger)1920, length - offset)]);
    }
}

- (void)captureOutput:(AVCaptureOutput *)output didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer
       fromConnection:(AVCaptureConnection *)connection {
    CVPixelBufferRef pixels = CMSampleBufferGetImageBuffer(sampleBuffer);
    if (!pixels && [output isKindOfClass:AVCaptureAudioDataOutput.class]) {
        [self sendMicrophoneSample:sampleBuffer];
        return;
    }
    void (^handler)(CVPixelBufferRef) = self.onFrame;
    if (pixels && handler) handler(pixels);
}

@end
