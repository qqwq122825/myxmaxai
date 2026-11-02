//
//  ViewController.m
//  AIMoYan
//
//  One screen: camera preview, server address + Key, connect.
//  Tap = focus, double-tap = switch camera, pinch = zoom. When the server returns the
//  processed picture it fills the screen and the camera moves to the small window
//  (tap the small window to swap).
//

#import "ViewController.h"
#import <AVFoundation/AVFoundation.h>
#import <Security/Security.h>
#import "RGCamera.h"
#import "RGConnection.h"
#import "RGEncoder.h"
#import "RGReturnView.h"

#pragma mark - Palette (same as the AI魔颜 desktop app)

static UIColor *RGColor(uint32_t hex, CGFloat alpha) {
    return [UIColor colorWithRed:((hex >> 16) & 0xFF) / 255.0 green:((hex >> 8) & 0xFF) / 255.0
                            blue:(hex & 0xFF) / 255.0 alpha:alpha];
}
#define RG_INK      RGColor(0x1A1420, 1)
#define RG_PANEL    RGColor(0x231B2A, 0.92)
#define RG_RAISED   RGColor(0x2D2436, 1)
#define RG_LINE     RGColor(0x3B3046, 1)
#define RG_TEXT     RGColor(0xF4EDF6, 1)
#define RG_MUTED    RGColor(0xA797B1, 1)
#define RG_RING     RGColor(0xFFD6B0, 1)
#define RG_LIVE     RGColor(0xFF4F86, 1)
#define RG_OK       RGColor(0x5BD6A8, 1)

#pragma mark - Keychain (the Key is a credential, keep it out of UserDefaults)

static NSString *const kKeychainService = @"AIMoYan.server";
static NSString *const kAddressDefaultsKey = @"AIMoYan.serverAddress";
static NSString *const kQualityDefaultsKey = @"AIMoYan.quality";
static NSString *const kPortraitDefaultsKey = @"AIMoYan.portrait";

static void RGKeychainSave(NSString *value) {
    NSDictionary *query = @{(id)kSecClass: (id)kSecClassGenericPassword, (id)kSecAttrService: kKeychainService,
                            (id)kSecAttrAccount: @"key"};
    SecItemDelete((__bridge CFDictionaryRef)query);
    if (value.length == 0) return;
    NSMutableDictionary *item = [query mutableCopy];
    item[(id)kSecValueData] = [value dataUsingEncoding:NSUTF8StringEncoding];
    item[(id)kSecAttrAccessible] = (id)kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly;
    SecItemAdd((__bridge CFDictionaryRef)item, NULL);
}

static NSString *RGKeychainLoad(void) {
    NSDictionary *query = @{(id)kSecClass: (id)kSecClassGenericPassword, (id)kSecAttrService: kKeychainService,
                            (id)kSecAttrAccount: @"key", (id)kSecReturnData: @YES, (id)kSecMatchLimit: (id)kSecMatchLimitOne};
    CFTypeRef result = NULL;
    if (SecItemCopyMatching((__bridge CFDictionaryRef)query, &result) != errSecSuccess || !result) return @"";
    NSData *data = CFBridgingRelease(result);
    return [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding] ?: @"";
}

#pragma mark - Small views

/// UIView whose backing layer is the camera preview.
@interface RGPreviewView : UIView
@property (nonatomic, readonly) AVCaptureVideoPreviewLayer *previewLayer;
@end
@implementation RGPreviewView
+ (Class)layerClass { return AVCaptureVideoPreviewLayer.class; }
- (AVCaptureVideoPreviewLayer *)previewLayer { return (AVCaptureVideoPreviewLayer *)self.layer; }
@end

typedef NS_ENUM(NSInteger, RGState) { RGStateIdle, RGStateConnecting, RGStateLive };

@interface ViewController () <RGConnectionDelegate, UITextFieldDelegate, UIGestureRecognizerDelegate>
// Media
@property (nonatomic, strong) RGCamera *camera;
@property (atomic, strong, nullable) RGConnection *connection;
@property (atomic, strong, nullable) RGEncoder *encoder;
// Views
@property (nonatomic, strong) RGPreviewView *previewView;
@property (nonatomic, strong) RGReturnView *returnView;
@property (nonatomic, strong) UIView *focusSquare;
@property (nonatomic, strong) UIView *exposureTrack;        // vertical line next to the focus box
@property (nonatomic, strong) UIImageView *exposureSun;     // ☀ knob, moves with the brightness
@property (nonatomic, strong) UILabel *lockLabel;           // "AE/AF 锁定"
@property (nonatomic) float exposureBias;
@property (nonatomic) float panStartBias;
@property (nonatomic, strong, nullable) NSTimer *focusHideTimer;
@property (nonatomic, strong) UIView *statusPill;
@property (nonatomic, strong) UIView *statusDot;
@property (nonatomic, strong) UILabel *statusLabel;
@property (nonatomic, strong) UILabel *statsLabel;
@property (nonatomic, strong) UIButton *switchButton;
@property (nonatomic, strong) UIButton *qualityButton;
@property (nonatomic, strong) UIButton *orientationButton;
@property (nonatomic, strong) UIView *panel;
@property (nonatomic, strong) UIStackView *formStack;
@property (nonatomic, strong) UITextField *addressField;
@property (nonatomic, strong) UITextField *keyField;
@property (nonatomic, strong) UIButton *connectButton;
@property (nonatomic, strong) UILabel *hintLabel;
// State
@property (nonatomic) RGState state;
@property (nonatomic) RGQuality quality;
@property (nonatomic) BOOL portrait;
@property (nonatomic) BOOL returnActive;
@property (nonatomic) BOOL localIsBig;           // user swapped the windows
@property (nonatomic, strong, nullable) NSTimer *statsTimer;
@property (nonatomic) uint64_t lastSent, lastSentBytes, lastReceived;
@end

@implementation ViewController

- (UIStatusBarStyle)preferredStatusBarStyle { return UIStatusBarStyleLightContent; }
- (UIInterfaceOrientationMask)supportedInterfaceOrientations { return UIInterfaceOrientationMaskPortrait; }

- (void)viewDidLoad {
    [super viewDidLoad];
    self.view.backgroundColor = RG_INK;
    self.overrideUserInterfaceStyle = UIUserInterfaceStyleDark;

    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    self.quality = [defaults integerForKey:kQualityDefaultsKey] == RGQuality1080p ? RGQuality1080p : RGQuality720p;
    self.portrait = [defaults objectForKey:kPortraitDefaultsKey] ? [defaults boolForKey:kPortraitDefaultsKey] : YES;

    [self buildViews];
    [self buildGestures];
    self.addressField.text = [defaults stringForKey:kAddressDefaultsKey];
    self.keyField.text = RGKeychainLoad();
    [self applyState:RGStateIdle];
    [self refreshOptionButtons];

    self.camera = [RGCamera new];
    self.previewView.previewLayer.session = self.camera.session;
    self.camera.previewLayer = self.previewView.previewLayer;
    __weak typeof(self) weakSelf = self;
    self.camera.onFrame = ^(CVPixelBufferRef pixelBuffer) { [weakSelf handleCameraFrame:pixelBuffer]; };
    [self startCamera];

    [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(appDidEnterBackground)
                                               name:UIApplicationDidEnterBackgroundNotification object:nil];
}

- (void)dealloc {
    [NSNotificationCenter.defaultCenter removeObserver:self];
}

#pragma mark Camera

- (void)startCamera {
    AVAuthorizationStatus status = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
    if (status == AVAuthorizationStatusNotDetermined) {
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo completionHandler:^(BOOL granted) {
            dispatch_async(dispatch_get_main_queue(), ^{ [self startCamera]; });
        }];
        return;
    }
    if (status != AVAuthorizationStatusAuthorized) {
        [self setStatus:@"没有相机权限" color:RG_LIVE];
        UIAlertController *alert = [UIAlertController alertControllerWithTitle:@"需要相机权限"
            message:@"请在 设置 → AI魔颜 中打开相机权限。" preferredStyle:UIAlertControllerStyleAlert];
        [alert addAction:[UIAlertAction actionWithTitle:@"去设置" style:UIAlertActionStyleDefault handler:^(UIAlertAction *a) {
            [UIApplication.sharedApplication openURL:[NSURL URLWithString:UIApplicationOpenSettingsURLString] options:@{} completionHandler:nil];
        }]];
        [alert addAction:[UIAlertAction actionWithTitle:@"取消" style:UIAlertActionStyleCancel handler:nil]];
        [self presentViewController:alert animated:YES completion:nil];
        return;
    }
    [self.camera startWithPosition:AVCaptureDevicePositionBack quality:self.quality portrait:self.portrait completion:^(NSError *error) {
        if (error) [self setStatus:error.localizedDescription color:RG_LIVE];
    }];
}

/// Capture queue. Drop frames rather than queue them when the uplink is busy (keeps delay low).
- (void)handleCameraFrame:(CVPixelBufferRef)pixelBuffer {
    RGConnection *connection = self.connection;
    RGEncoder *encoder = self.encoder;
    if (!connection || !encoder || !connection.ready) return;
    if (![connection canSendVideo]) { [connection noteDroppedFrame]; return; }
    [encoder encodePixelBuffer:pixelBuffer];
}

#pragma mark Connect

- (void)connectTapped {
    [self.view endEditing:YES];
    if (self.state != RGStateIdle) { [self disconnectWithMessage:@"已断开"]; return; }

    NSError *error = nil;
    RGServerInfo *server = [RGServerInfo infoWithAddress:self.addressField.text ?: @"" key:self.keyField.text ?: @"" error:&error];
    if (!server) { [self showError:error.localizedDescription]; return; }
    [NSUserDefaults.standardUserDefaults setObject:self.addressField.text forKey:kAddressDefaultsKey];
    RGKeychainSave(self.keyField.text);

    CGSize size = [RGCamera frameSizeForQuality:self.quality portrait:self.portrait];
    RGVideoSettings *settings = [RGVideoSettings new];
    settings.width = (int)size.width;
    settings.height = (int)size.height;
    settings.fps = 30;
    settings.bitrateKbps = self.quality == RGQuality1080p ? 8000 : 4000;

    RGConnection *connection = [[RGConnection alloc] initWithServer:server settings:settings];
    connection.delegate = self;
    self.connection = connection;
    [self applyState:RGStateConnecting];
    [self setStatus:[NSString stringWithFormat:@"正在连接 %@:%ld …", server.host, (long)server.port] color:RG_RING];
    [connection connect];
}

- (void)disconnectWithMessage:(NSString *)message {
    RGConnection *connection = self.connection;
    self.connection = nil;
    connection.delegate = nil;
    [connection disconnect];
    [self.encoder invalidate];
    self.encoder = nil;
    [self.returnView reset];
    self.returnActive = NO;
    [self applyState:RGStateIdle];
    [self setStatus:message color:RG_MUTED];
    [self layoutVideoAnimated:YES];
}

- (void)appDidEnterBackground {
    // iOS stops the camera in the background; end the session cleanly instead of letting it time out.
    if (self.state != RGStateIdle) [self disconnectWithMessage:@"已切到后台，连接已断开"];
}

#pragma mark RGConnectionDelegate

- (void)connectionDidBecomeReady:(RGConnection *)connection {
    if (connection != self.connection) return;
    CGSize size = [RGCamera frameSizeForQuality:self.quality portrait:self.portrait];
    RGEncoder *encoder = [[RGEncoder alloc] initWithWidth:(int)size.width height:(int)size.height
                                                      fps:30 bitrateKbps:self.quality == RGQuality1080p ? 8000 : 4000];
    if (!encoder) { [self disconnectWithMessage:@"这台手机无法启动 H.264 编码"]; return; }
    __weak RGConnection *weakConnection = connection;
    encoder.onMessage = ^(NSData *message) { [weakConnection sendVideoMessage:message]; };
    self.encoder = encoder;
    self.lastSent = self.lastSentBytes = self.lastReceived = 0;
    [self applyState:RGStateLive];
    [self setStatus:@"推流中 · 换脸结果在电脑端接收" color:RG_OK];
}

- (void)connection:(RGConnection *)connection returnActiveChanged:(BOOL)active {
    // Push-only: the phone never shows the processed picture (it goes to the PC).
    if (connection != self.connection || !active) return;
    // Each resumed return stream starts with a new keyframe.
    [self.returnView reset];
    self.returnActive = active;
    self.localIsBig = NO;
    [self setStatus:active ? @"已连接 · 正在接收处理后的画面" : @"已连接 · 等待服务器开始回传" color:RG_OK];
    [self layoutVideoAnimated:YES];
}

- (void)connection:(RGConnection *)connection didReceiveVideo:(NSData *)message {
    if (connection != self.connection) return;
    [self.returnView enqueueMessage:message];
}

- (void)connection:(RGConnection *)connection didCloseWithError:(NSError *)error {
    if (connection != self.connection) return;
    [self disconnectWithMessage:error ? @"连接已断开" : @"已断开"];
    if (error) [self showError:error.localizedDescription];
}

#pragma mark Stats

- (void)tickStats {
    RGConnection *c = self.connection;
    if (!c || self.state != RGStateLive) { self.statsLabel.text = @""; return; }
    uint64_t sent = c.sentFrames, bytes = c.sentBytes, received = c.receivedFrames;
    double upFps = sent - self.lastSent, mbps = (bytes - self.lastSentBytes) * 8.0 / 1e6, downFps = received - self.lastReceived;
    self.lastSent = sent; self.lastSentBytes = bytes; self.lastReceived = received;
    NSString *rtt = c.rttMs >= 0 ? [NSString stringWithFormat:@"%.0f ms", c.rttMs] : @"—";
    CGSize size = [RGCamera frameSizeForQuality:self.quality portrait:self.portrait];
    (void)downFps;
    self.statsLabel.text = [NSString stringWithFormat:@"%.0f×%.0f  推流 %.0f fps  %.1f Mbps   网络延迟 %@",
                            size.width, size.height, upFps, mbps, rtt];
}

#pragma mark UI state

- (void)applyState:(RGState)state {
    _state = state;
    BOOL idle = state == RGStateIdle;
    self.addressField.enabled = self.keyField.enabled = idle;
    self.qualityButton.enabled = self.orientationButton.enabled = idle;
    self.qualityButton.alpha = self.orientationButton.alpha = idle ? 1 : 0.4;

    NSString *title = state == RGStateIdle ? @"连接" : state == RGStateConnecting ? @"连接中…（点击取消）" : @"断开";
    UIColor *color = state == RGStateLive ? RG_RAISED : RG_LIVE;
    UIButtonConfiguration *config = self.connectButton.configuration;
    config.title = title;
    config.baseBackgroundColor = color;
    UIBackgroundConfiguration *background = config.background;
    background.strokeColor = state == RGStateLive ? RG_LIVE : UIColor.clearColor;
    background.strokeWidth = state == RGStateLive ? 1 : 0;
    config.background = background;
    self.connectButton.configuration = config;

    [UIView animateWithDuration:0.25 animations:^{
        self.formStack.hidden = !idle;
        self.formStack.alpha = idle ? 1 : 0;
        self.hintLabel.hidden = !idle;
    }];
    UIApplication.sharedApplication.idleTimerDisabled = !idle;   // keep the screen on while streaming

    [self.statsTimer invalidate];
    self.statsTimer = nil;
    if (state == RGStateLive) {
        __weak typeof(self) weakSelf = self;
        self.statsTimer = [NSTimer scheduledTimerWithTimeInterval:1 repeats:YES block:^(NSTimer *t) { [weakSelf tickStats]; }];
    } else {
        self.statsLabel.text = @"";
    }
}

- (void)setStatus:(NSString *)text color:(UIColor *)color {
    self.statusLabel.text = text;
    self.statusDot.backgroundColor = color;
}

- (void)showError:(NSString *)message {
    UIAlertController *alert = [UIAlertController alertControllerWithTitle:@"连接失败" message:message
                                                            preferredStyle:UIAlertControllerStyleAlert];
    [alert addAction:[UIAlertAction actionWithTitle:@"好" style:UIAlertActionStyleDefault handler:nil]];
    if (!self.presentedViewController) [self presentViewController:alert animated:YES completion:nil];
}

- (void)showToast:(NSString *)text {
    UILabel *toast = [UILabel new];
    toast.text = [NSString stringWithFormat:@"  %@  ", text];
    toast.textColor = RG_INK;
    toast.backgroundColor = RG_TEXT;
    toast.font = [UIFont systemFontOfSize:14 weight:UIFontWeightMedium];
    toast.layer.cornerRadius = 16;
    toast.layer.masksToBounds = YES;
    toast.textAlignment = NSTextAlignmentCenter;
    [toast sizeToFit];
    toast.frame = CGRectMake(0, 0, toast.bounds.size.width + 12, 32);
    toast.center = CGPointMake(CGRectGetMidX(self.view.bounds), CGRectGetMidY(self.view.bounds));
    toast.alpha = 0;
    [self.view addSubview:toast];
    [UIView animateWithDuration:0.2 animations:^{ toast.alpha = 1; } completion:^(BOOL f) {
        [UIView animateWithDuration:0.3 delay:1.2 options:0 animations:^{ toast.alpha = 0; } completion:^(BOOL g) { [toast removeFromSuperview]; }];
    }];
}

#pragma mark Options (camera / resolution / orientation)

- (void)switchCameraTapped {
    [self.camera switchCamera];
    [self setLocked:NO];
    self.exposureBias = 0;
    [self showToast:self.camera.position == AVCaptureDevicePositionBack ? @"前置摄像头" : @"后置摄像头"];
}

- (void)setQualityOption:(RGQuality)quality portrait:(BOOL)portrait {
    if (self.state != RGStateIdle) { [self showToast:@"请先断开再修改"]; return; }
    self.quality = quality;
    self.portrait = portrait;
    [NSUserDefaults.standardUserDefaults setInteger:quality forKey:kQualityDefaultsKey];
    [NSUserDefaults.standardUserDefaults setBool:portrait forKey:kPortraitDefaultsKey];
    [self.camera setQuality:quality portrait:portrait];
    [self refreshOptionButtons];
    CGSize size = [RGCamera frameSizeForQuality:quality portrait:portrait];
    [self showToast:[NSString stringWithFormat:@"%.0f × %.0f", size.width, size.height]];
}

- (void)refreshOptionButtons {
    __weak typeof(self) weakSelf = self;
    NSArray *qualities = @[@[@"720p", @(RGQuality720p)], @[@"1080p（服务器需支持）", @(RGQuality1080p)]];
    NSMutableArray *qualityActions = [NSMutableArray array];
    for (NSArray *q in qualities) {
        RGQuality value = [q[1] integerValue];
        UIAction *action = [UIAction actionWithTitle:q[0] image:nil identifier:nil handler:^(UIAction *a) {
            [weakSelf setQualityOption:value portrait:weakSelf.portrait];
        }];
        action.state = value == self.quality ? UIMenuElementStateOn : UIMenuElementStateOff;
        [qualityActions addObject:action];
    }
    self.qualityButton.menu = [UIMenu menuWithTitle:@"分辨率" children:qualityActions];
    [self setLabel:self.quality == RGQuality1080p ? @"1080" : @"720" onButton:self.qualityButton];

    UIAction *portraitAction = [UIAction actionWithTitle:@"竖屏" image:[UIImage systemImageNamed:@"rectangle.portrait"] identifier:nil
        handler:^(UIAction *a) { [weakSelf setQualityOption:weakSelf.quality portrait:YES]; }];
    UIAction *landscapeAction = [UIAction actionWithTitle:@"横屏（手机横着拿）" image:[UIImage systemImageNamed:@"rectangle"] identifier:nil
        handler:^(UIAction *a) { [weakSelf setQualityOption:weakSelf.quality portrait:NO]; }];
    portraitAction.state = self.portrait ? UIMenuElementStateOn : UIMenuElementStateOff;
    landscapeAction.state = self.portrait ? UIMenuElementStateOff : UIMenuElementStateOn;
    self.orientationButton.menu = [UIMenu menuWithTitle:@"画面方向" children:@[portraitAction, landscapeAction]];
    [self setLabel:self.portrait ? @"竖屏" : @"横屏" onButton:self.orientationButton];
}

- (void)setLabel:(NSString *)label onButton:(UIButton *)button {
    UIButtonConfiguration *config = button.configuration;
    NSDictionary *attrs = @{NSFontAttributeName: [UIFont systemFontOfSize:11 weight:UIFontWeightSemibold]};
    config.attributedTitle = [[NSAttributedString alloc] initWithString:label attributes:attrs];
    button.configuration = config;
}

#pragma mark Gestures: tap = focus, drag = brightness, long-press = lock, double-tap = switch camera

- (void)buildGestures {
    UITapGestureRecognizer *doubleTap = [[UITapGestureRecognizer alloc] initWithTarget:self action:@selector(handleDoubleTap:)];
    doubleTap.numberOfTapsRequired = 2;
    UITapGestureRecognizer *tap = [[UITapGestureRecognizer alloc] initWithTarget:self action:@selector(handleTap:)];
    [tap requireGestureRecognizerToFail:doubleTap];
    UILongPressGestureRecognizer *longPress = [[UILongPressGestureRecognizer alloc] initWithTarget:self action:@selector(handleLongPress:)];
    longPress.minimumPressDuration = 0.5;
    UIPanGestureRecognizer *pan = [[UIPanGestureRecognizer alloc] initWithTarget:self action:@selector(handleExposurePan:)];
    pan.maximumNumberOfTouches = 1;
    for (UIGestureRecognizer *g in @[doubleTap, tap, longPress, pan]) {
        g.delegate = self;
        [self.view addGestureRecognizer:g];
    }
}

- (BOOL)gestureRecognizerShouldBegin:(UIGestureRecognizer *)gesture {
    // Brightness dragging only while the focus box is showing (right after a tap / long-press).
    if ([gesture isKindOfClass:UIPanGestureRecognizer.class]) return self.focusSquare.alpha > 0.01 && self.isLocalBig;
    if ([gesture isKindOfClass:UILongPressGestureRecognizer.class]) return self.isLocalBig;
    return YES;
}

- (BOOL)gestureRecognizer:(UIGestureRecognizer *)gesture shouldReceiveTouch:(UITouch *)touch {
    // Ignore touches on the panel and buttons.
    UIView *v = touch.view;
    while (v && v != self.view) {
        if (v == self.panel || [v isKindOfClass:UIControl.class] || v == self.statusPill) return NO;
        v = v.superview;
    }
    return YES;
}

- (BOOL)isLocalBig {
    return !self.returnActive || self.localIsBig;
}

- (void)handleTap:(UITapGestureRecognizer *)tap {
    if (self.addressField.isFirstResponder || self.keyField.isFirstResponder) {
        [self.view endEditing:YES];
        return;
    }
    CGPoint point = [tap locationInView:self.view];
    UIView *small = self.isLocalBig ? self.returnView : self.previewView;
    if (self.returnActive && CGRectContainsPoint(small.frame, point)) {   // tap the small window: swap
        self.localIsBig = !self.localIsBig;
        [self layoutVideoAnimated:YES];
        return;
    }
    if (!self.isLocalBig) return;                                         // focus only on the camera picture
    CGPoint inPreview = [self.view convertPoint:point toView:self.previewView];
    CGPoint devicePoint = [self.previewView.previewLayer captureDevicePointOfInterestForPoint:inPreview];
    [self.camera focusAtDevicePoint:devicePoint];
    self.exposureBias = 0;
    [self setLocked:NO];
    [self showFocusAt:point];
}

- (void)handleLongPress:(UILongPressGestureRecognizer *)press {
    if (press.state != UIGestureRecognizerStateBegan) return;
    CGPoint point = [press locationInView:self.view];
    CGPoint inPreview = [self.view convertPoint:point toView:self.previewView];
    [self.camera lockFocusAndExposureAtDevicePoint:[self.previewView.previewLayer captureDevicePointOfInterestForPoint:inPreview]];
    self.exposureBias = 0;
    [self showFocusAt:point];
    [self setLocked:YES];
    [[[UIImpactFeedbackGenerator alloc] initWithStyle:UIImpactFeedbackStyleMedium] impactOccurred];
}

- (void)setLocked:(BOOL)locked {
    [UIView animateWithDuration:0.2 animations:^{ self.lockLabel.alpha = locked ? 1 : 0; }];
}

/// Drag up = brighter, down = darker (≈ 1 EV per 150 pt), like the iPhone Camera app.
- (void)handleExposurePan:(UIPanGestureRecognizer *)pan {
    if (pan.state == UIGestureRecognizerStateBegan) self.panStartBias = self.exposureBias;
    CGFloat dy = [pan translationInView:self.view].y;
    self.exposureBias = [self.camera setExposureBias:self.panStartBias - (float)(dy / 150.0)];
    [self layoutExposureKnob];
    [self keepFocusUIVisible];
}

- (void)layoutExposureKnob {
    CGRect track = self.exposureTrack.frame;
    CGFloat t = (2.0 - self.exposureBias) / 4.0;            // +2 EV at the top, -2 EV at the bottom
    self.exposureSun.center = CGPointMake(CGRectGetMidX(track), CGRectGetMinY(track) + t * CGRectGetHeight(track));
}

- (void)keepFocusUIVisible {
    [self.focusHideTimer invalidate];
    for (UIView *v in @[self.focusSquare, self.exposureTrack, self.exposureSun]) { [v.layer removeAllAnimations]; v.alpha = 1; }
    __weak typeof(self) weakSelf = self;
    self.focusHideTimer = [NSTimer scheduledTimerWithTimeInterval:2.0 repeats:NO block:^(NSTimer *t) {
        [UIView animateWithDuration:0.4 animations:^{
            weakSelf.focusSquare.alpha = 0; weakSelf.exposureTrack.alpha = 0; weakSelf.exposureSun.alpha = 0;
        }];
    }];
}

- (void)handleDoubleTap:(UITapGestureRecognizer *)tap {
    [self switchCameraTapped];
}

- (void)showFocusAt:(CGPoint)point {
    UIView *square = self.focusSquare;
    square.center = point;
    // Slider sits to the right of the box (left side if the box is near the right edge).
    CGFloat side = point.x + 64 < CGRectGetMaxX(self.view.bounds) - 20 ? 1 : -1;
    self.exposureTrack.frame = CGRectMake(point.x + side * 58 - 0.75, point.y - 70, 1.5, 140);
    [self layoutExposureKnob];
    [self keepFocusUIVisible];
    square.transform = CGAffineTransformMakeScale(1.4, 1.4);
    [UIView animateWithDuration:0.25 animations:^{ square.transform = CGAffineTransformIdentity; }];
}

#pragma mark Layout

- (void)viewDidLayoutSubviews {
    [super viewDidLayoutSubviews];
    [self layoutVideoAnimated:NO];
}

- (void)layoutVideoAnimated:(BOOL)animated {
    CGRect full = self.view.bounds;
    CGFloat pipWidth = 116;
    CGFloat pipHeight = self.portrait ? pipWidth * 16.0 / 9.0 : pipWidth * 9.0 / 16.0;
    CGFloat top = self.view.safeAreaInsets.top + 64;
    CGRect pip = CGRectMake(CGRectGetMaxX(full) - pipWidth - 14, top, pipWidth, pipHeight);

    void (^apply)(void) = ^{
        if (!self.returnActive) {
            self.previewView.frame = full;
            self.returnView.frame = pip;
            self.returnView.alpha = 0;
        } else if (self.localIsBig) {
            self.previewView.frame = full;
            self.returnView.frame = pip;
            self.returnView.alpha = 1;
        } else {
            self.returnView.frame = full;
            self.previewView.frame = pip;
            self.returnView.alpha = 1;
        }
        UIView *small = self.isLocalBig ? self.returnView : self.previewView;
        UIView *big = small == self.returnView ? self.previewView : self.returnView;
        small.layer.cornerRadius = 12;
        small.layer.borderWidth = 1.5;
        small.layer.borderColor = [UIColor colorWithWhite:1 alpha:0.75].CGColor;
        big.layer.cornerRadius = 0;
        big.layer.borderWidth = 0;
        [self.view insertSubview:big atIndex:0];
        [self.view insertSubview:small aboveSubview:big];
    };
    if (animated) [UIView animateWithDuration:0.3 animations:apply]; else apply();
}

#pragma mark Build views

- (UIButton *)roundButtonWithSymbol:(NSString *)symbol {
    UIButtonConfiguration *config = [UIButtonConfiguration filledButtonConfiguration];
    config.image = [UIImage systemImageNamed:symbol withConfiguration:
                    [UIImageSymbolConfiguration configurationWithPointSize:18 weight:UIImageSymbolWeightSemibold]];
    config.imagePlacement = NSDirectionalRectEdgeTop;
    config.imagePadding = 2;
    config.baseBackgroundColor = RGColor(0x1A1420, 0.6);
    config.baseForegroundColor = RG_TEXT;
    config.cornerStyle = UIButtonConfigurationCornerStyleLarge;
    config.contentInsets = NSDirectionalEdgeInsetsMake(8, 6, 8, 6);
    UIButton *button = [UIButton buttonWithConfiguration:config primaryAction:nil];
    button.translatesAutoresizingMaskIntoConstraints = NO;
    [button.widthAnchor constraintEqualToConstant:54].active = YES;
    [button.heightAnchor constraintEqualToConstant:54].active = YES;
    return button;
}

- (UITextField *)fieldWithPlaceholder:(NSString *)placeholder {
    UITextField *field = [UITextField new];
    field.attributedPlaceholder = [[NSAttributedString alloc] initWithString:placeholder attributes:@{NSForegroundColorAttributeName: RG_MUTED}];
    field.textColor = RG_TEXT;
    field.font = [UIFont systemFontOfSize:15];
    field.backgroundColor = RG_INK;
    field.layer.cornerRadius = 10;
    field.layer.borderColor = RG_LINE.CGColor;
    field.layer.borderWidth = 1;
    field.autocorrectionType = UITextAutocorrectionTypeNo;
    field.autocapitalizationType = UITextAutocapitalizationTypeNone;
    field.spellCheckingType = UITextSpellCheckingTypeNo;
    field.leftView = [[UIView alloc] initWithFrame:CGRectMake(0, 0, 12, 1)];
    field.leftViewMode = UITextFieldViewModeAlways;
    field.clearButtonMode = UITextFieldViewModeWhileEditing;
    field.delegate = self;
    [field.heightAnchor constraintEqualToConstant:46].active = YES;
    return field;
}

- (UILabel *)captionWithText:(NSString *)text {
    UILabel *label = [UILabel new];
    label.text = text;
    label.textColor = RG_MUTED;
    label.font = [UIFont systemFontOfSize:12];
    return label;
}

- (void)buildViews {
    // Video
    self.previewView = [RGPreviewView new];
    self.previewView.previewLayer.videoGravity = AVLayerVideoGravityResizeAspectFill;
    self.previewView.backgroundColor = UIColor.blackColor;
    self.previewView.clipsToBounds = YES;
    self.returnView = [RGReturnView new];
    self.returnView.clipsToBounds = YES;
    [self.view addSubview:self.previewView];
    [self.view addSubview:self.returnView];

    self.focusSquare = [[UIView alloc] initWithFrame:CGRectMake(0, 0, 76, 76)];
    self.focusSquare.layer.borderColor = RG_RING.CGColor;
    self.focusSquare.layer.borderWidth = 1.5;
    self.focusSquare.layer.cornerRadius = 6;
    self.focusSquare.alpha = 0;
    self.focusSquare.userInteractionEnabled = NO;
    [self.view addSubview:self.focusSquare];

    self.exposureTrack = [UIView new];
    self.exposureTrack.backgroundColor = [RG_RING colorWithAlphaComponent:0.7];
    self.exposureTrack.alpha = 0;
    self.exposureTrack.userInteractionEnabled = NO;
    [self.view addSubview:self.exposureTrack];
    self.exposureSun = [[UIImageView alloc] initWithImage:[UIImage systemImageNamed:@"sun.max.fill"
        withConfiguration:[UIImageSymbolConfiguration configurationWithPointSize:20 weight:UIImageSymbolWeightBold]]];
    self.exposureSun.tintColor = RG_RING;
    self.exposureSun.alpha = 0;
    self.exposureSun.userInteractionEnabled = NO;
    [self.view addSubview:self.exposureSun];

    self.lockLabel = [UILabel new];
    self.lockLabel.text = @"  AE/AF 锁定  ";
    self.lockLabel.font = [UIFont systemFontOfSize:13 weight:UIFontWeightSemibold];
    self.lockLabel.textColor = RG_INK;
    self.lockLabel.backgroundColor = RG_RING;
    self.lockLabel.layer.cornerRadius = 6;
    self.lockLabel.layer.masksToBounds = YES;
    self.lockLabel.alpha = 0;
    self.lockLabel.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:self.lockLabel];


    // Status pill (top)
    self.statusPill = [UIView new];
    self.statusPill.backgroundColor = RGColor(0x1A1420, 0.65);
    self.statusPill.layer.cornerRadius = 16;
    self.statusPill.translatesAutoresizingMaskIntoConstraints = NO;
    self.statusDot = [UIView new];
    self.statusDot.layer.cornerRadius = 4;
    self.statusDot.translatesAutoresizingMaskIntoConstraints = NO;
    self.statusLabel = [UILabel new];
    self.statusLabel.textColor = RG_TEXT;
    self.statusLabel.font = [UIFont systemFontOfSize:13 weight:UIFontWeightMedium];
    self.statusLabel.translatesAutoresizingMaskIntoConstraints = NO;
    [self.statusPill addSubview:self.statusDot];
    [self.statusPill addSubview:self.statusLabel];
    [self.view addSubview:self.statusPill];
    [self setStatus:@"AI魔颜 · 未连接" color:RG_MUTED];

    self.statsLabel = [UILabel new];
    self.statsLabel.textColor = RG_TEXT;
    self.statsLabel.font = [UIFont monospacedDigitSystemFontOfSize:11 weight:UIFontWeightRegular];
    self.statsLabel.shadowColor = [UIColor colorWithWhite:0 alpha:0.7];
    self.statsLabel.shadowOffset = CGSizeMake(0, 1);
    self.statsLabel.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:self.statsLabel];

    // Side buttons
    self.switchButton = [self roundButtonWithSymbol:@"arrow.triangle.2.circlepath.camera"];
    [self.switchButton addTarget:self action:@selector(switchCameraTapped) forControlEvents:UIControlEventTouchUpInside];
    self.qualityButton = [self roundButtonWithSymbol:@"sparkles.tv"];
    self.qualityButton.showsMenuAsPrimaryAction = YES;
    self.orientationButton = [self roundButtonWithSymbol:@"rotate.right"];
    self.orientationButton.showsMenuAsPrimaryAction = YES;
    UIStackView *side = [[UIStackView alloc] initWithArrangedSubviews:@[self.switchButton, self.qualityButton, self.orientationButton]];
    side.axis = UILayoutConstraintAxisVertical;
    side.spacing = 12;
    side.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:side];

    // Bottom panel
    self.panel = [UIView new];
    self.panel.backgroundColor = RG_PANEL;
    self.panel.layer.cornerRadius = 22;
    self.panel.layer.borderColor = RG_LINE.CGColor;
    self.panel.layer.borderWidth = 1;
    self.panel.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:self.panel];

    self.addressField = [self fieldWithPlaceholder:@"服务器地址，例如 1.2.3.4:8766"];
    self.addressField.keyboardType = UIKeyboardTypeURL;
    self.addressField.returnKeyType = UIReturnKeyNext;
    self.keyField = [self fieldWithPlaceholder:@"连接 Key（RGK1. 开头）"];
    self.keyField.secureTextEntry = YES;
    self.keyField.returnKeyType = UIReturnKeyGo;

    UIButtonConfiguration *pasteConfig = [UIButtonConfiguration tintedButtonConfiguration];
    pasteConfig.title = @"粘贴";
    pasteConfig.baseForegroundColor = RG_RING;
    pasteConfig.baseBackgroundColor = RG_RING;
    UIButton *paste = [UIButton buttonWithConfiguration:pasteConfig primaryAction:[UIAction actionWithHandler:^(UIAction *a) {
        NSString *text = UIPasteboard.generalPasteboard.string;
        if (text.length) self.keyField.text = [text stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    }]];
    [paste.widthAnchor constraintEqualToConstant:64].active = YES;
    UIStackView *keyRow = [[UIStackView alloc] initWithArrangedSubviews:@[self.keyField, paste]];
    keyRow.spacing = 8;

    self.formStack = [[UIStackView alloc] initWithArrangedSubviews:@[
        [self captionWithText:@"服务器地址"], self.addressField,
        [self captionWithText:@"连接 Key"], keyRow]];
    self.formStack.axis = UILayoutConstraintAxisVertical;
    self.formStack.spacing = 6;
    [self.formStack setCustomSpacing:14 afterView:self.addressField];

    UIButtonConfiguration *connectConfig = [UIButtonConfiguration filledButtonConfiguration];
    connectConfig.title = @"连接";
    connectConfig.baseBackgroundColor = RG_LIVE;
    connectConfig.baseForegroundColor = UIColor.whiteColor;
    connectConfig.cornerStyle = UIButtonConfigurationCornerStyleLarge;
    connectConfig.titleTextAttributesTransformer = ^NSDictionary *(NSDictionary *incoming) {
        NSMutableDictionary *out = [incoming mutableCopy];
        out[NSFontAttributeName] = [UIFont systemFontOfSize:17 weight:UIFontWeightBold];
        return out;
    };
    self.connectButton = [UIButton buttonWithConfiguration:connectConfig primaryAction:nil];
    [self.connectButton addTarget:self action:@selector(connectTapped) forControlEvents:UIControlEventTouchUpInside];
    [self.connectButton.heightAnchor constraintEqualToConstant:52].active = YES;

    self.hintLabel = [self captionWithText:@"轻点对焦后上下滑调亮度 · 长按锁定对焦和亮度 · 双击切换前后摄像头"];
    self.hintLabel.textAlignment = NSTextAlignmentCenter;
    self.hintLabel.font = [UIFont systemFontOfSize:11];

    UIStackView *panelStack = [[UIStackView alloc] initWithArrangedSubviews:@[self.formStack, self.connectButton, self.hintLabel]];
    panelStack.axis = UILayoutConstraintAxisVertical;
    panelStack.spacing = 14;
    panelStack.translatesAutoresizingMaskIntoConstraints = NO;
    [self.panel addSubview:panelStack];

    UILayoutGuide *safe = self.view.safeAreaLayoutGuide;
    [NSLayoutConstraint activateConstraints:@[
        [self.statusPill.topAnchor constraintEqualToAnchor:safe.topAnchor constant:8],
        [self.statusPill.leadingAnchor constraintEqualToAnchor:safe.leadingAnchor constant:14],
        [self.statusPill.heightAnchor constraintEqualToConstant:32],
        [self.statusPill.trailingAnchor constraintLessThanOrEqualToAnchor:safe.trailingAnchor constant:-14],
        [self.statusDot.leadingAnchor constraintEqualToAnchor:self.statusPill.leadingAnchor constant:12],
        [self.statusDot.centerYAnchor constraintEqualToAnchor:self.statusPill.centerYAnchor],
        [self.statusDot.widthAnchor constraintEqualToConstant:8],
        [self.statusDot.heightAnchor constraintEqualToConstant:8],
        [self.statusLabel.leadingAnchor constraintEqualToAnchor:self.statusDot.trailingAnchor constant:8],
        [self.statusLabel.trailingAnchor constraintEqualToAnchor:self.statusPill.trailingAnchor constant:-14],
        [self.statusLabel.centerYAnchor constraintEqualToAnchor:self.statusPill.centerYAnchor],

        [self.statsLabel.topAnchor constraintEqualToAnchor:self.statusPill.bottomAnchor constant:6],
        [self.statsLabel.leadingAnchor constraintEqualToAnchor:self.statusPill.leadingAnchor constant:4],

        [self.lockLabel.centerXAnchor constraintEqualToAnchor:self.view.centerXAnchor],
        [self.lockLabel.topAnchor constraintEqualToAnchor:safe.topAnchor constant:52],
        [self.lockLabel.heightAnchor constraintEqualToConstant:26],

        [side.trailingAnchor constraintEqualToAnchor:safe.trailingAnchor constant:-14],
        [side.bottomAnchor constraintEqualToAnchor:self.panel.topAnchor constant:-16],

        [self.panel.leadingAnchor constraintEqualToAnchor:safe.leadingAnchor constant:12],
        [self.panel.trailingAnchor constraintEqualToAnchor:safe.trailingAnchor constant:-12],
        [self.panel.bottomAnchor constraintEqualToAnchor:self.view.keyboardLayoutGuide.topAnchor constant:-10],
        [panelStack.topAnchor constraintEqualToAnchor:self.panel.topAnchor constant:16],
        [panelStack.leadingAnchor constraintEqualToAnchor:self.panel.leadingAnchor constant:16],
        [panelStack.trailingAnchor constraintEqualToAnchor:self.panel.trailingAnchor constant:-16],
        [panelStack.bottomAnchor constraintEqualToAnchor:self.panel.bottomAnchor constant:-12],
    ]];
}

#pragma mark UITextFieldDelegate

- (BOOL)textFieldShouldReturn:(UITextField *)textField {
    if (textField == self.addressField) [self.keyField becomeFirstResponder];
    else { [textField resignFirstResponder]; [self connectTapped]; }
    return YES;
}

@end
