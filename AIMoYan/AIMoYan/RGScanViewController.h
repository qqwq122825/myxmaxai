//
//  RGScanViewController.h
//  AIMoYan
//
//  Full-screen QR scanner (back camera) with a "从相册识别" fallback for screenshots of the code.
//  Uses its own capture session: stop the main camera before presenting, resume it in completion.
//

#import <UIKit/UIKit.h>

NS_ASSUME_NONNULL_BEGIN

@interface RGScanViewController : UIViewController
/// Called once on the main queue: the QR text, or nil when the user cancelled.
@property (nonatomic, copy, nullable) void (^completion)(NSString *_Nullable text);
@end

NS_ASSUME_NONNULL_END
