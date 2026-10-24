import Foundation

struct Credentials: Codable, Sendable {
    let baseURL: String
    let token: String
}

/// Pi URL and token, stored in the app's sandbox container with 0600.
///
/// This deliberately replaces the keychain. The keychain binds its "always
/// allow" to the app's signature, which changes with every ad-hoc build, so it
/// asked again after each rebuild and eventually refused to store at all. The
/// token grants read-only access to household power data on the LAN; a file
/// only this app's container (and the user's own processes) can reach is a
/// proportionate place for it.
enum CredentialStore {
    static var url: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory,
                                            in: .userDomainMask)[0]
        return base.appendingPathComponent("credentials.json")
    }

    static func read() -> Credentials? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(Credentials.self, from: data)
    }

    @discardableResult
    static func write(_ credentials: Credentials) -> Bool {
        do {
            let directory = url.deletingLastPathComponent()
            try FileManager.default.createDirectory(at: directory,
                                                    withIntermediateDirectories: true)
            try JSONEncoder().encode(credentials).write(to: url, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600],
                                                  ofItemAtPath: url.path)
            return true
        } catch {
            return false
        }
    }
}
