//
//  RGReturnView.h
//  AIMoYan
//
//  Shows the processed picture returned by the server ("RGV1" + H.264 Annex-B) with
//  AVSampleBufferDisplayLayer, which decodes in hardware.
//

#import <UIKit/UIKit.h>

NS_ASSUME_NONNULL_BEGIN

@interface RGReturnView : UIView
/// Number of frames shown since the last reset.
@property (atomic, readonly) uint64_t framesShown;
- (void)enqueueMessage:(NSData *)message;
/// Call when the server restarts its return stream (it begins with a fresh keyframe).
- (void)reset;
@end

NS_ASSUME_NONNULL_END
