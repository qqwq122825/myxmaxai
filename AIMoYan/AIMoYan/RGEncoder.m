//
//  RGEncoder.m
//  AIMoYan
//

#import "RGEncoder.h"
#import <VideoToolbox/VideoToolbox.h>
#import <QuartzCore/QuartzCore.h>

static const uint8_t kStartCode[4] = {0, 0, 0, 1};

@interface RGEncoder ()
@property (nonatomic, readwrite) int width, height;
@property (nonatomic) int fps;
@property (nonatomic) VTCompressionSessionRef session;
@property (nonatomic) VTPixelTransferSessionRef transfer;
@property (nonatomic) CVPixelBufferPoolRef pool;
@property (nonatomic) CFTimeInterval origin;
@property (nonatomic) int64_t lastPts;
@property (nonatomic) BOOL invalidated;
@end

@implementation RGEncoder

- (nullable instancetype)initWithWidth:(int)width height:(int)height fps:(int)fps bitrateKbps:(int)kbps {
    if (!(self = [super init])) return nil;
    _width = width; _height = height; _fps = fps;
    _origin = CACurrentMediaTime();
    _lastPts = -1;

    VTCompressionSessionRef session = NULL;
    OSStatus status = VTCompressionSessionCreate(kCFAllocatorDefault, width, height, kCMVideoCodecType_H264,
                                                 NULL, NULL, NULL, NULL, NULL, &session);
    if (status != noErr || !session) return nil;
    _session = session;

    VTSessionSetProperty(session, kVTCompressionPropertyKey_RealTime, kCFBooleanTrue);
    VTSessionSetProperty(session, kVTCompressionPropertyKey_ProfileLevel, kVTProfileLevel_H264_High_AutoLevel);
    VTSessionSetProperty(session, kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse);   // no B-frames
    VTSessionSetProperty(session, kVTCompressionPropertyKey_ExpectedFrameRate, (__bridge CFNumberRef)@(fps));
    VTSessionSetProperty(session, kVTCompressionPropertyKey_MaxKeyFrameInterval, (__bridge CFNumberRef)@(fps));
    VTSessionSetProperty(session, kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration, (__bridge CFNumberRef)@1);
    VTSessionSetProperty(session, kVTCompressionPropertyKey_AverageBitRate, (__bridge CFNumberRef)@(kbps * 1000));
    // Hard cap at 1.5x the target per second so a busy scene cannot flood the uplink.
    NSArray *limits = @[@(kbps * 1000 * 3 / 2 / 8), @1];
    VTSessionSetProperty(session, kVTCompressionPropertyKey_DataRateLimits, (__bridge CFArrayRef)limits);
    VTCompressionSessionPrepareToEncodeFrames(session);
    return self;
}

- (void)dealloc {
    [self invalidate];
}

- (void)invalidate {
    @synchronized (self) {
        if (self.invalidated) return;
        self.invalidated = YES;
        if (_session) {
            VTCompressionSessionInvalidate(_session);
            CFRelease(_session);
            _session = NULL;
        }
        if (_transfer) { VTPixelTransferSessionInvalidate(_transfer); CFRelease(_transfer); _transfer = NULL; }
        if (_pool) { CVPixelBufferPoolRelease(_pool); _pool = NULL; }
    }
}

/// Returns a +1 pixel buffer of exactly width x height.
- (nullable CVPixelBufferRef)copyFittedBuffer:(CVPixelBufferRef)source {
    if ((int)CVPixelBufferGetWidth(source) == self.width && (int)CVPixelBufferGetHeight(source) == self.height) {
        return CVPixelBufferRetain(source);
    }
    if (!_transfer) {
        if (VTPixelTransferSessionCreate(kCFAllocatorDefault, &_transfer) != noErr) return NULL;
        VTSessionSetProperty(_transfer, kVTPixelTransferPropertyKey_ScalingMode, kVTScalingMode_CropSourceToCleanAperture);
    }
    if (!_pool) {
        NSDictionary *attrs = @{(id)kCVPixelBufferPixelFormatTypeKey: @(CVPixelBufferGetPixelFormatType(source)),
                                (id)kCVPixelBufferWidthKey: @(self.width),
                                (id)kCVPixelBufferHeightKey: @(self.height),
                                (id)kCVPixelBufferIOSurfacePropertiesKey: @{}};
        CVPixelBufferPoolCreate(kCFAllocatorDefault, NULL, (__bridge CFDictionaryRef)attrs, &_pool);
    }
    CVPixelBufferRef out = NULL;
    if (!_pool || CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, _pool, &out) != kCVReturnSuccess) return NULL;
    if (VTPixelTransferSessionTransferImage(_transfer, source, out) != noErr) { CVPixelBufferRelease(out); return NULL; }
    return out;
}

- (void)encodePixelBuffer:(CVPixelBufferRef)pixelBuffer {
    @synchronized (self) {
        if (self.invalidated || !_session) return;
        CVPixelBufferRef fitted = [self copyFittedBuffer:pixelBuffer];
        if (!fitted) return;

        // Timestamps follow the wall clock (the server checks that queue delay is not growing).
        int64_t pts = (int64_t)((CACurrentMediaTime() - self.origin) * 90000.0);
        if (pts <= self.lastPts) pts = self.lastPts + 1;
        self.lastPts = pts;

        int width = self.width, height = self.height;
        __weak typeof(self) weakSelf = self;
        VTCompressionSessionEncodeFrameWithOutputHandler(_session, fitted, CMTimeMake(pts, 90000), kCMTimeInvalid, NULL, NULL,
            ^(OSStatus status, VTEncodeInfoFlags infoFlags, CMSampleBufferRef sampleBuffer) {
            if (status != noErr || !sampleBuffer || (infoFlags & kVTEncodeInfo_FrameDropped)) return;
            NSData *message = [RGEncoder messageFromSampleBuffer:sampleBuffer pts:pts width:width height:height];
            __strong typeof(weakSelf) self = weakSelf;
            if (message && self.onMessage) self.onMessage(message);
        });
        CVPixelBufferRelease(fitted);
    }
}

+ (nullable NSData *)messageFromSampleBuffer:(CMSampleBufferRef)sampleBuffer pts:(int64_t)pts width:(int)width height:(int)height {
    if (!CMSampleBufferDataIsReady(sampleBuffer)) return nil;

    NSMutableData *out = [NSMutableData dataWithCapacity:64 * 1024];
    // Header: "RGV1" | int64 pts | uint16 width | uint16 height, all big-endian ("!4sqHH").
    [out appendBytes:"RGV1" length:4];
    uint64_t beP = CFSwapInt64HostToBig((uint64_t)pts);
    uint16_t beW = CFSwapInt16HostToBig((uint16_t)width), beH = CFSwapInt16HostToBig((uint16_t)height);
    [out appendBytes:&beP length:8];
    [out appendBytes:&beW length:2];
    [out appendBytes:&beH length:2];

    // Keyframe? Prepend SPS/PPS so the server decoder can start (or resync) here.
    BOOL keyframe = YES;
    CFArrayRef attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, false);
    if (attachments && CFArrayGetCount(attachments) > 0) {
        CFDictionaryRef dict = (CFDictionaryRef)CFArrayGetValueAtIndex(attachments, 0);
        CFBooleanRef notSync = NULL;
        if (CFDictionaryGetValueIfPresent(dict, kCMSampleAttachmentKey_NotSync, (const void **)&notSync)) {
            keyframe = !(notSync && CFBooleanGetValue(notSync));
        }
    }
    int nalLength = 4;
    CMFormatDescriptionRef format = CMSampleBufferGetFormatDescription(sampleBuffer);
    if (format) {
        size_t count = 0;
        int headerLength = 4;
        if (CMVideoFormatDescriptionGetH264ParameterSetAtIndex(format, 0, NULL, NULL, &count, &headerLength) == noErr) {
            nalLength = headerLength;
            if (keyframe) {
                for (size_t i = 0; i < count; i++) {
                    const uint8_t *ps = NULL; size_t size = 0;
                    if (CMVideoFormatDescriptionGetH264ParameterSetAtIndex(format, i, &ps, &size, NULL, NULL) == noErr) {
                        [out appendBytes:kStartCode length:4];
                        [out appendBytes:ps length:size];
                    }
                }
            }
        }
    }

    // AVCC (length-prefixed) → Annex-B (start codes).
    CMBlockBufferRef block = CMSampleBufferGetDataBuffer(sampleBuffer);
    size_t total = block ? CMBlockBufferGetDataLength(block) : 0;
    if (total == 0) return nil;
    NSMutableData *avcc = [NSMutableData dataWithLength:total];
    if (CMBlockBufferCopyDataBytes(block, 0, total, avcc.mutableBytes) != kCMBlockBufferNoErr) return nil;
    const uint8_t *p = avcc.bytes;
    size_t offset = 0;
    while (offset + (size_t)nalLength <= total) {
        uint32_t len = 0;
        for (int i = 0; i < nalLength; i++) len = (len << 8) | p[offset + i];
        offset += nalLength;
        if (len == 0 || offset + len > total) break;
        [out appendBytes:kStartCode length:4];
        [out appendBytes:p + offset length:len];
        offset += len;
    }
    return out.length > 16 ? out : nil;
}

@end
