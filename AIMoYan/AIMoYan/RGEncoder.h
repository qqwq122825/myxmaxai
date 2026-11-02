//
//  RGEncoder.h
//  AIMoYan
//
//  Hardware H.264 encoder (VideoToolbox). Emits complete "RGV1" messages:
//  "RGV1" | int64 pts (90 kHz, big-endian) | uint16 width | uint16 height | H.264 Annex-B access unit
//  Every keyframe carries SPS/PPS, matching the server's libx264 "repeat-headers" stream.
//

#import <Foundation/Foundation.h>
#import <CoreVideo/CoreVideo.h>

NS_ASSUME_NONNULL_BEGIN

@interface RGEncoder : NSObject
@property (nonatomic, readonly) int width, height;
/// Called on a VideoToolbox thread for every encoded frame.
@property (nonatomic, copy, nullable) void (^onMessage)(NSData *message);

- (nullable instancetype)initWithWidth:(int)width height:(int)height fps:(int)fps bitrateKbps:(int)kbps;
/// Encodes one camera frame. Frames of a different size are scaled to the encoder size first.
- (void)encodePixelBuffer:(CVPixelBufferRef)pixelBuffer;
- (void)invalidate;
@end

NS_ASSUME_NONNULL_END
