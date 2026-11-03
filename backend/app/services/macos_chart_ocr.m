#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <Vision/Vision.h>
#include <stdio.h>

static void fail(NSString *message) {
    fprintf(stderr, "%s", message.UTF8String ?: "local Vision OCR failed\n");
    exit(1);
}

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        if (argc != 2) {
            fail(@"usage: macos_chart_ocr <image-path>\n");
        }

        NSURL *imageURL = [NSURL fileURLWithPath:[NSString stringWithUTF8String:argv[1]]];
        NSImage *image = [[NSImage alloc] initWithContentsOfURL:imageURL];
        CGImageRef cgImage = [image CGImageForProposedRect:nil context:nil hints:nil];
        if (cgImage == nil) {
            fail(@"unable to decode image\n");
        }

        [NSApplication sharedApplication];

        VNRecognizeTextRequest *request =
            [[VNRecognizeTextRequest alloc] initWithCompletionHandler:nil];
        // Revision 3's accurate model stalls in Apple Neural Engine model
        // compilation on this host. Revision 1's fast model completes within
        // the worker's bounded OCR deadline and still returns coordinates.
        request.recognitionLevel = VNRequestTextRecognitionLevelFast;
        request.revision = VNRecognizeTextRequestRevision1;

        VNImageRequestHandler *handler =
            [[VNImageRequestHandler alloc] initWithURL:imageURL options:@{}];
        NSError *error = nil;
        BOOL didPerform = [handler performRequests:@[ request ] error:&error];
        if (!didPerform && request.results == nil) {
            fail([NSString stringWithFormat:@"Vision OCR failed: %@\n", error.localizedDescription]);
        }

        NSMutableArray<NSDictionary *> *tokens = [NSMutableArray array];
        for (VNRecognizedTextObservation *observation in request.results ?: @[]) {
            VNRecognizedText *candidate = [[observation topCandidates:1] firstObject];
            if (candidate == nil) {
                continue;
            }
            CGRect box = observation.boundingBox;
            [tokens addObject:@{
                @"text": candidate.string,
                @"confidence": @(candidate.confidence),
                @"x": @(box.origin.x),
                @"y": @(box.origin.y),
                @"width": @(box.size.width),
                @"height": @(box.size.height),
            }];
        }

        NSData *json = [NSJSONSerialization dataWithJSONObject:tokens options:0 error:&error];
        if (json == nil) {
            fail([NSString stringWithFormat:@"could not encode OCR output: %@\n", error.localizedDescription]);
        }
        fwrite(json.bytes, 1, json.length, stdout);
    }
    return 0;
}
