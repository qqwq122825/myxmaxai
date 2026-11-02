//
//  ViewController.h
//  AIMoYan
//
//  Created by xx on 2026/11/2.
//

#import <UIKit/UIKit.h>

@interface ViewController : UIViewController

/// aimoyan://connect?… opened from outside (system Camera app scanned the QR code). Fills in and connects.
- (void)handlePairingURL:(NSURL *)url;

@end

