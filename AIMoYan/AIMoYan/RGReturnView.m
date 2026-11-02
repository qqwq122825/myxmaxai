//
//  RGReturnView.m
//  AIMoYan
//

#import "RGReturnView.h"
#import <AVFoundation/AVFoundation.h>

@interface RGReturnView ()
@property (atomic, readwrite) uint64_t framesShown;
@property (nonatomic, strong) dispatch_queue_t queue;
@property (nonatomic, copy, nullable) NSData *sps;
@property (nonatomic, copy, nullable) NSData *pps;
@property (nonatomic, nullable) CMVideoFormatDescriptionRef format;
@property (nonatomic) int64_t lastPts;
@end

@implementation RGReturnView

+ (Class)layerClass {
    return AVSampleBufferDisplayLayer.class;
}

- (AVSampleBufferDisplayLayer *)displayLayer {
    return (AVSampleBufferDisplayLayer *)self.layer;
}

- (instancetype)initWithFrame:(CGRect)frame {
    if ((self = [super initWithFrame:frame])) {
        self.backgroundColor = UIColor.blackColor;
        self.displayLayer.videoGravity = AVLayerVideoGravityResizeAspect;
        _queue = dispatch_queue_create("AIMoYan.return-video", DISPATCH_QUEUE_SERIAL);
        _lastPts = -1;
    }
    return self;
}

- (void)dealloc {
    if (_format) CFRelease(_format);
}

- (void)reset {
    dispatch_async(self.queue, ^{
        self.sps = nil; self.pps = nil;
        if (self.format) { CFRelease(self.format); self.format = NULL; }
        self.lastPts = -1;
        self.framesShown = 0;
        if (@available(iOS 17.0, *)) {
            [self.displayLayer.sampleBufferRenderer flushWithRemovalOfDisplayedImage:YES completionHandler:nil];
        } else {
            [self.displayLayer flushAndRemoveImage];
        }
    });
}

- (void)enqueueMessage:(NSData *)message {
    dispatch_async(self.queue, ^{ [self handleMessage:message]; });
}

- (void)handleMessage:(NSData *)message {
    if (message.length <= 16) return;
    const uint8_t *bytes = message.bytes;
    if (memcmp(bytes, "RGV1", 4) != 0) return;
    uint64_t bePts; memcpy(&bePts, bytes + 4, 8);
    int64_t pts = (int64_t)CFSwapInt64BigToHost(bePts);
    if (pts <= self.lastPts) return;            // never go backwards
    self.lastPts = pts;

    // Split the Annex-B access unit into NAL units.
    const uint8_t *p = bytes + 16;
    size_t length = message.length - 16;
    NSMutableArray<NSData *> *nals = [NSMutableArray array];
    size_t i = 0, start = SIZE_MAX;
    while (i + 3 <= length) {
        size_t codeLength = 0;
        if (p[i] == 0 && p[i + 1] == 0 && p[i + 2] == 1) codeLength = 3;
        else if (i + 4 <= length && p[i] == 0 && p[i + 1] == 0 && p[i + 2] == 0 && p[i + 3] == 1) codeLength = 4;
        if (codeLength) {
            if (start != SIZE_MAX && i > start) [nals addObject:[NSData dataWithBytes:p + start length:i - start]];
            i += codeLength;
            start = i;
        } else {
            i++;
        }
    }
    if (start != SIZE_MAX && start < length) [nals addObject:[NSData dataWithBytes:p + start length:length - start]];

    NSMutableData *avcc = [NSMutableData data];
    BOOL parametersChanged = NO;
    for (NSData *nal in nals) {
        if (nal.length == 0) continue;
        uint8_t type = ((const uint8_t *)nal.bytes)[0] & 0x1F;
        if (type == 7) { if (![nal isEqualToData:self.sps]) { self.sps = nal; parametersChanged = YES; } }
        else if (type == 8) { if (![nal isEqualToData:self.pps]) { self.pps = nal; parametersChanged = YES; } }
        else if (type == 1 || type == 5) {
            uint32_t be = CFSwapInt32HostToBig((uint32_t)nal.length);
            [avcc appendBytes:&be length:4];
            [avcc appendData:nal];
        }
    }
    if (parametersChanged && self.sps && self.pps) [self rebuildFormat];
    if (!self.format || avcc.length == 0) return;   // wait for the first keyframe with SPS/PPS

    CMBlockBufferRef block = NULL;
    if (CMBlockBufferCreateWithMemoryBlock(kCFAllocatorDefault, NULL, avcc.length, kCFAllocatorDefault, NULL, 0,
                                           avcc.length, kCMBlockBufferAssureMemoryNowFlag, &block) != kCMBlockBufferNoErr) return;
    CMBlockBufferReplaceDataBytes(avcc.bytes, block, 0, avcc.length);

    CMSampleBufferRef sample = NULL;
    size_t sampleSize = avcc.length;
    CMSampleTimingInfo timing = {kCMTimeInvalid, CMTimeMake(pts, 90000), kCMTimeInvalid};
    OSStatus status = CMSampleBufferCreateReady(kCFAllocatorDefault, block, self.format, 1, 1, &timing, 1, &sampleSize, &sample);
    CFRelease(block);
    if (status != noErr || !sample) return;

    CFArrayRef attachments = CMSampleBufferGetSampleAttachmentsArray(sample, true);
    if (attachments && CFArrayGetCount(attachments) > 0) {
        CFMutableDictionaryRef dict = (CFMutableDictionaryRef)CFArrayGetValueAtIndex(attachments, 0);
        CFDictionarySetValue(dict, kCMSampleAttachmentKey_DisplayImmediately, kCFBooleanTrue);
    }

    if (@available(iOS 17.0, *)) {
        AVSampleBufferVideoRenderer *renderer = self.displayLayer.sampleBufferRenderer;
        if (renderer.status == AVQueuedSampleBufferRenderingStatusFailed) [renderer flush];
        [renderer enqueueSampleBuffer:sample];
    } else {
        AVSampleBufferDisplayLayer *layer = self.displayLayer;
        if (layer.status == AVQueuedSampleBufferRenderingStatusFailed) [layer flush];
        [layer enqueueSampleBuffer:sample];
    }
    CFRelease(sample);
    self.framesShown += 1;
}

- (void)rebuildFormat {
    if (self.format) { CFRelease(self.format); self.format = NULL; }
    const uint8_t *sets[2] = {self.sps.bytes, self.pps.bytes};
    size_t sizes[2] = {self.sps.length, self.pps.length};
    CMVideoFormatDescriptionRef format = NULL;
    if (CMVideoFormatDescriptionCreateFromH264ParameterSets(kCFAllocatorDefault, 2, sets, sizes, 4, &format) == noErr) {
        self.format = format;
    }
}

@end
