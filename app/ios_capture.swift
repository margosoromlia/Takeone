// ios_capture.swift — хелпер Takeone для macOS.
// Показывает экран iPhone/iPad, подключённого по USB, и пишет его в .mov
// с настоящей паузой (AVCaptureMovieFileOutput.pauseRecording/resumeRecording).
//
//   ios_capture --list              -> строки "uniqueID<TAB>имя"
//   ios_capture --device <uniqueID> -> окно трансляции; команды в stdin:
//        start <путь.mov> | pause | resume | stop | quit
//   Ответы в stdout: ready | started | paused | resumed | stopped | error <текст>

import AppKit
import AVFoundation
import CoreMediaIO

func reply(_ s: String) {
    print(s)
    fflush(stdout)
}

// Разрешаем «экранные» устройства (iPhone/iPad по USB) — как это делает QuickTime.
func enableScreenCaptureDevices() {
    var prop = CMIOObjectPropertyAddress(
        mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyAllowScreenCaptureDevices),
        mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
        mElement: CMIOObjectPropertyElement(0))
    var allow: UInt32 = 1
    CMIOObjectSetPropertyData(CMIOObjectID(kCMIOObjectSystemObject), &prop, 0, nil,
                              UInt32(MemoryLayout<UInt32>.size), &allow)
}

func screenDevices() -> [AVCaptureDevice] {
    let types: [AVCaptureDevice.DeviceType]
    if #available(macOS 14.0, *) {
        types = [.external]
    } else {
        types = [.externalUnknown]
    }
    return AVCaptureDevice.DiscoverySession(deviceTypes: types, mediaType: .muxed,
                                            position: .unspecified).devices
}

func waitForDevices(seconds: Double) -> [AVCaptureDevice] {
    let end = Date().addingTimeInterval(seconds)
    var devs = screenDevices()
    while devs.isEmpty && Date() < end {
        RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        devs = screenDevices()
    }
    // даём шанс появиться остальным устройствам
    RunLoop.current.run(until: Date().addingTimeInterval(0.5))
    return screenDevices()
}

func ensureCameraAccess() -> Bool {
    switch AVCaptureDevice.authorizationStatus(for: .video) {
    case .authorized:
        return true
    case .notDetermined:
        let sem = DispatchSemaphore(value: 0)
        var granted = false
        AVCaptureDevice.requestAccess(for: .video) { ok in
            granted = ok
            sem.signal()
        }
        sem.wait()
        return granted
    default:
        return false
    }
}

final class Recorder: NSObject, AVCaptureFileOutputRecordingDelegate {
    let session = AVCaptureSession()
    let output = AVCaptureMovieFileOutput()
    var window: NSWindow!

    init(device: AVCaptureDevice) throws {
        super.init()
        let input = try AVCaptureDeviceInput(device: device)
        session.beginConfiguration()
        if session.canAddInput(input) { session.addInput(input) }
        if session.canAddOutput(output) { session.addOutput(output) }
        session.commitConfiguration()

        let preview = AVCaptureVideoPreviewLayer(session: session)
        preview.videoGravity = .resizeAspect
        preview.backgroundColor = NSColor.black.cgColor

        window = NSWindow(contentRect: NSRect(x: 200, y: 120, width: 420, height: 860),
                          styleMask: [.titled, .resizable, .miniaturizable],
                          backing: .buffered, defer: false)
        window.title = "Takeone — \(device.localizedName)"
        window.isReleasedWhenClosed = false
        let view = NSView(frame: window.contentView!.bounds)
        view.layer = preview
        view.wantsLayer = true
        window.contentView = view
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        session.startRunning()
    }

    func handle(_ line: String) {
        let parts = line.split(separator: " ", maxSplits: 1).map(String.init)
        switch parts.first ?? "" {
        case "start":
            guard parts.count == 2 else { reply("error не указан путь"); return }
            let url = URL(fileURLWithPath: parts[1])
            try? FileManager.default.removeItem(at: url)
            output.startRecording(to: url, recordingDelegate: self)
        case "pause":
            if output.isRecording { output.pauseRecording() }
            reply("paused")
        case "resume":
            if output.isRecording { output.resumeRecording() }
            reply("resumed")
        case "stop":
            if output.isRecording { output.stopRecording() } else { reply("stopped") }
        case "quit":
            if output.isRecording { output.stopRecording() }
            session.stopRunning()
            exit(0)
        default:
            reply("error неизвестная команда: \(line)")
        }
    }

    func fileOutput(_ output: AVCaptureFileOutput, didStartRecordingTo fileURL: URL,
                    from connections: [AVCaptureConnection]) {
        reply("started")
    }

    func fileOutput(_ output: AVCaptureFileOutput, didFinishRecordingTo outputFileURL: URL,
                    from connections: [AVCaptureConnection], error: Error?) {
        if let e = error as NSError?,
           (e.userInfo[AVErrorRecordingSuccessfullyFinishedKey] as? Bool) != true,
           !FileManager.default.fileExists(atPath: outputFileURL.path) {
            reply("error запись не сохранилась: \(e.localizedDescription)")
            return
        }
        reply("stopped")
    }
}

// ------------------------------------------------------------------ main
let args = CommandLine.arguments
enableScreenCaptureDevices()

if args.contains("--list") {
    for d in waitForDevices(seconds: 4) {
        print("\(d.uniqueID)\t\(d.localizedName)")
    }
    exit(0)
}

guard let i = args.firstIndex(of: "--device"), i + 1 < args.count else {
    reply("error использование: ios_capture --list | --device <id>")
    exit(1)
}
let wanted = args[i + 1]
guard let device = waitForDevices(seconds: 6).first(where: { $0.uniqueID == wanted }) else {
    reply("error устройство не найдено — разблокируйте iPhone и переподключите кабель")
    exit(1)
}
guard ensureCameraAccess() else {
    reply("error нет доступа к камере. Системные настройки → Конфиденциальность и безопасность → Камера → включите для Терминала (или приложения, из которого запущен Takeone)")
    exit(1)
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)

let recorder: Recorder
do {
    recorder = try Recorder(device: device)
} catch {
    reply("error не удалось открыть устройство: \(error.localizedDescription)")
    exit(1)
}
reply("ready")

DispatchQueue.global().async {
    while let line = readLine() {
        let cmd = line.trimmingCharacters(in: .whitespacesAndNewlines)
        if cmd.isEmpty { continue }
        DispatchQueue.main.async { recorder.handle(cmd) }
    }
    DispatchQueue.main.async { recorder.handle("quit") }
}

app.run()
