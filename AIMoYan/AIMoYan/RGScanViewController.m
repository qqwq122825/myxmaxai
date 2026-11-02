//
//  RGScanViewController.m
//  AIMoYan
//

#import "RGScanViewController.h"
#import <AVFoundation/AVFoundation.h>
#import <CoreImage/CoreImage.h>
#import <PhotosUI/PhotosUI.h>

@interface RGScanViewController () <AVCaptureMetadataOutputObjectsDelegate, PHPickerViewControllerDelegate>
@property (nonatomic, strong) AVCaptureSession *session;
@property (nonatomic, strong) dispatch_queue_t sessionQueue;
@property (nonatomic, strong) AVCaptureVideoPreviewLayer *previewLayer;
@property (nonatomic, strong) UIView *frameView;
@property (nonatomic, strong) UILabel *messageLabel;
@property (nonatomic) BOOL finished;
@end

@implementation RGScanViewController

- (UIStatusBarStyle)preferredStatusBarStyle { return UIStatusBarStyleLightContent; }
- (UIInterfaceOrientationMask)supportedInterfaceOrientations { return UIInterfaceOrientationMaskPortrait; }

- (void)viewDidLoad {
    [super viewDidLoad];
    self.view.backgroundColor = UIColor.blackColor;
    self.overrideUserInterfaceStyle = UIUserInterfaceStyleDark;
    self.sessionQueue = dispatch_queue_create("AIMoYan.scan.session", DISPATCH_QUEUE_SERIAL);

    self.session = [AVCaptureSession new];
    self.previewLayer = [AVCaptureVideoPreviewLayer layerWithSession:self.session];
    self.previewLayer.videoGravity = AVLayerVideoGravityResizeAspectFill;
    [self.view.layer addSublayer:self.previewLayer];

    // Dimmed surroundings + bright square in the middle
    self.frameView = [UIView new];
    self.frameView.layer.borderColor = [UIColor colorWithRed:1 green:0.84 blue:0.69 alpha:1].CGColor;
    self.frameView.layer.borderWidth = 3;
    self.frameView.layer.cornerRadius = 18;
    self.frameView.userInteractionEnabled = NO;
    [self.view addSubview:self.frameView];

    UILabel *title = [UILabel new];
    title.text = @"扫描连接二维码";
    title.textColor = UIColor.whiteColor;
    title.font = [UIFont systemFontOfSize:20 weight:UIFontWeightBold];
    title.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:title];

    self.messageLabel = [UILabel new];
    self.messageLabel.text = @"电脑接收端（或服务器）上点「显示二维码」，把二维码放进框里";
    self.messageLabel.textColor = [UIColor colorWithWhite:1 alpha:0.85];
    self.messageLabel.font = [UIFont systemFontOfSize:14];
    self.messageLabel.numberOfLines = 0;
    self.messageLabel.textAlignment = NSTextAlignmentCenter;
    self.messageLabel.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:self.messageLabel];

    UIButtonConfiguration *closeConfig = [UIButtonConfiguration filledButtonConfiguration];
    closeConfig.image = [UIImage systemImageNamed:@"xmark"];
    closeConfig.baseBackgroundColor = [UIColor colorWithWhite:0 alpha:0.5];
    closeConfig.baseForegroundColor = UIColor.whiteColor;
    closeConfig.cornerStyle = UIButtonConfigurationCornerStyleCapsule;
    UIButton *close = [UIButton buttonWithConfiguration:closeConfig primaryAction:[UIAction actionWithHandler:^(UIAction *a) {
        [self finishWith:nil];
    }]];
    close.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:close];

    UIButtonConfiguration *albumConfig = [UIButtonConfiguration filledButtonConfiguration];
    albumConfig.title = @"从相册识别";
    albumConfig.image = [UIImage systemImageNamed:@"photo.on.rectangle"];
    albumConfig.imagePadding = 6;
    albumConfig.baseBackgroundColor = [UIColor colorWithWhite:0 alpha:0.55];
    albumConfig.baseForegroundColor = UIColor.whiteColor;
    albumConfig.cornerStyle = UIButtonConfigurationCornerStyleCapsule;
    UIButton *album = [UIButton buttonWithConfiguration:albumConfig primaryAction:[UIAction actionWithHandler:^(UIAction *a) {
        [self pickFromAlbum];
    }]];
    album.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:album];

    UILayoutGuide *safe = self.view.safeAreaLayoutGuide;
    [NSLayoutConstraint activateConstraints:@[
        [close.topAnchor constraintEqualToAnchor:safe.topAnchor constant:10],
        [close.trailingAnchor constraintEqualToAnchor:safe.trailingAnchor constant:-16],
        [close.widthAnchor constraintEqualToConstant:44],
        [close.heightAnchor constraintEqualToConstant:44],
        [title.centerXAnchor constraintEqualToAnchor:self.view.centerXAnchor],
        [title.topAnchor constraintEqualToAnchor:safe.topAnchor constant:70],
        [self.messageLabel.topAnchor constraintEqualToAnchor:title.bottomAnchor constant:10],
        [self.messageLabel.leadingAnchor constraintEqualToAnchor:safe.leadingAnchor constant:32],
        [self.messageLabel.trailingAnchor constraintEqualToAnchor:safe.trailingAnchor constant:-32],
        [album.centerXAnchor constraintEqualToAnchor:self.view.centerXAnchor],
        [album.bottomAnchor constraintEqualToAnchor:safe.bottomAnchor constant:-36],
        [album.heightAnchor constraintEqualToConstant:46],
    ]];

    [self startCamera];
}

- (void)viewDidLayoutSubviews {
    [super viewDidLayoutSubviews];
    self.previewLayer.frame = self.view.bounds;
    CGFloat side = MIN(CGRectGetWidth(self.view.bounds) - 80, 280);
    self.frameView.frame = CGRectMake(0, 0, side, side);
    self.frameView.center = CGPointMake(CGRectGetMidX(self.view.bounds), CGRectGetMidY(self.view.bounds));
}

- (void)startCamera {
    AVAuthorizationStatus status = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
    if (status != AVAuthorizationStatusAuthorized) {
        self.messageLabel.text = @"没有相机权限，可以点下面「从相册识别」，选择二维码截图。";
        return;
    }
    dispatch_async(self.sessionQueue, ^{
        AVCaptureDevice *device = [AVCaptureDevice defaultDeviceWithDeviceType:AVCaptureDeviceTypeBuiltInWideAngleCamera
                                                                    mediaType:AVMediaTypeVideo position:AVCaptureDevicePositionBack];
        AVCaptureDeviceInput *input = device ? [AVCaptureDeviceInput deviceInputWithDevice:device error:nil] : nil;
        [self.session beginConfiguration];
        if (input && [self.session canAddInput:input]) [self.session addInput:input];
        AVCaptureMetadataOutput *output = [AVCaptureMetadataOutput new];
        if ([self.session canAddOutput:output]) {
            [self.session addOutput:output];
            [output setMetadataObjectsDelegate:self queue:dispatch_get_main_queue()];
            if ([output.availableMetadataObjectTypes containsObject:AVMetadataObjectTypeQRCode])
                output.metadataObjectTypes = @[AVMetadataObjectTypeQRCode];
        }
        [self.session commitConfiguration];
        if (device && [device lockForConfiguration:nil]) {
            if ([device isFocusModeSupported:AVCaptureFocusModeContinuousAutoFocus]) device.focusMode = AVCaptureFocusModeContinuousAutoFocus;
            if (device.isAutoFocusRangeRestrictionSupported) device.autoFocusRangeRestriction = AVCaptureAutoFocusRangeRestrictionNear;
            [device unlockForConfiguration];
        }
        [self.session startRunning];
        if (!input) dispatch_async(dispatch_get_main_queue(), ^{ self.messageLabel.text = @"打不开摄像头，可以点「从相册识别」。"; });
    });
}

- (void)stopCamera {
    AVCaptureSession *session = self.session;
    dispatch_async(self.sessionQueue, ^{ if (session.isRunning) [session stopRunning]; });
}

- (void)viewWillDisappear:(BOOL)animated {
    [super viewWillDisappear:animated];
    if (self.isBeingDismissed || self.finished) [self stopCamera];
}

#pragma mark Results

- (void)captureOutput:(AVCaptureOutput *)output didOutputMetadataObjects:(NSArray<__kindof AVMetadataObject *> *)objects
       fromConnection:(AVCaptureConnection *)connection {
    for (AVMetadataObject *object in objects) {
        if (![object isKindOfClass:AVMetadataMachineReadableCodeObject.class]) continue;
        NSString *text = [(AVMetadataMachineReadableCodeObject *)object stringValue];
        if (text.length) {
            [[UINotificationFeedbackGenerator new] notificationOccurred:UINotificationFeedbackTypeSuccess];
            [self finishWith:text];
            return;
        }
    }
}

- (void)finishWith:(nullable NSString *)text {
    if (self.finished) return;
    self.finished = YES;
    [self stopCamera];
    void (^completion)(NSString *) = self.completion;
    self.completion = nil;
    [self dismissViewControllerAnimated:YES completion:^{ if (completion) completion(text); }];
}

#pragma mark Album (PHPicker needs no photo-library permission)

- (void)pickFromAlbum {
    PHPickerConfiguration *config = [PHPickerConfiguration new];
    config.filter = PHPickerFilter.imagesFilter;
    config.selectionLimit = 1;
    PHPickerViewController *picker = [[PHPickerViewController alloc] initWithConfiguration:config];
    picker.delegate = self;
    [self presentViewController:picker animated:YES completion:nil];
}

- (void)picker:(PHPickerViewController *)picker didFinishPicking:(NSArray<PHPickerResult *> *)results {
    [picker dismissViewControllerAnimated:YES completion:nil];
    NSItemProvider *provider = results.firstObject.itemProvider;
    if (![provider canLoadObjectOfClass:UIImage.class]) return;
    [provider loadObjectOfClass:UIImage.class completionHandler:^(id<NSItemProviderReading> object, NSError *error) {
        NSString *text = [object isKindOfClass:UIImage.class] ? [RGScanViewController qrTextInImage:(UIImage *)object] : nil;
        dispatch_async(dispatch_get_main_queue(), ^{
            if (text.length) [self finishWith:text];
            else self.messageLabel.text = @"这张图片里没有识别到二维码，换一张更清晰的截图试试。";
        });
    }];
}

+ (nullable NSString *)qrTextInImage:(UIImage *)image {
    CIImage *ci = image.CGImage ? [CIImage imageWithCGImage:image.CGImage] : image.CIImage;
    if (!ci) return nil;
    CIDetector *detector = [CIDetector detectorOfType:CIDetectorTypeQRCode context:nil
                                              options:@{CIDetectorAccuracy: CIDetectorAccuracyHigh}];
    for (CIFeature *feature in [detector featuresInImage:ci]) {
        if ([feature isKindOfClass:CIQRCodeFeature.class] && [(CIQRCodeFeature *)feature messageString].length)
            return [(CIQRCodeFeature *)feature messageString];
    }
    return nil;
}

@end
