import SwiftUI

/// The popover is this app's only window. Without this, closing it counts as
/// "last window closed" and AppKit terminates the app.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // The Info.plist keys cover launch; these cover anything that re-enables
        // termination later. A menu bar app that sleeps between refreshes looks
        // idle to the system, and idle is exactly what it reclaims.
        ProcessInfo.processInfo.disableSuddenTermination()
        ProcessInfo.processInfo.disableAutomaticTermination("menu bar app stays resident")
    }
}

@main
struct EnergyMonitorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var state = AppState()

    var body: some Scene {
        MenuBarExtra {
            MenuBarContentView(state: state)
        } label: {
            Text(state.menuBarTitle).monospacedDigit()
        }
        .menuBarExtraStyle(.window)
    }
}
