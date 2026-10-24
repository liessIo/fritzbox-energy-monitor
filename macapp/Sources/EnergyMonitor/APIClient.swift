import Foundation

/// Read-only client for the Pi API. The FRITZ!Box is never contacted.
struct APIClient: Sendable {
    let baseURL: URL
    let token: String

    /// No disk cache: a stateless viewer must never serve a stale reading as
    /// current, and the container cache was throwing I/O errors anyway.
    private static let session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 10
        return URLSession(configuration: configuration)
    }()

    init?(credentials: Credentials) {
        guard let url = URL(string: Self.normalized(credentials.baseURL)),
              !credentials.token.isEmpty
        else { return nil }
        self.baseURL = url
        self.token = credentials.token
    }

    /// Accepts "raspberrypi.local:8000" as well as a full URL.
    static func normalized(_ raw: String) -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let withScheme = trimmed.contains("://") ? trimmed : "http://\(trimmed)"
        return withScheme.hasSuffix("/") ? String(withScheme.dropLast()) : withScheme
    }

    func latest() async throws -> LatestReading {
        try await get("/latest", query: [])
    }

    func history(from: Int, to: Int) async throws -> [HistoryPoint] {
        // No bucket parameter: the server picks one that keeps the response
        // small for the requested span.
        try await get("/history", query: [("from", "\(from)"), ("to", "\(to)")])
    }

    func devices() async throws -> [DeviceInfo] {
        try await get("/devices", query: [])
    }

    /// Separate from `history` on purpose: a plug measures a subset of the
    /// house total, so the two series must never be merged.
    func deviceHistory(device: String, from: Int, to: Int) async throws -> [HistoryPoint] {
        try await get("/device/history",
                      query: [("device", device), ("from", "\(from)"), ("to", "\(to)")])
    }

    func consumption(period: String, count: Int) async throws -> [ConsumptionEntry] {
        try await get("/consumption", query: [("period", period), ("n", "\(count)")])
    }

    func baseline(from: Int, to: Int) async throws -> BaselineInfo {
        try await get("/baseline", query: [("from", "\(from)"), ("to", "\(to)")])
    }

    private func get<T: Decodable>(_ path: String,
                                   query: [(String, String)]) async throws -> T {
        guard var components = URLComponents(string: baseURL.absoluteString + path) else {
            throw APIError.invalidURL(baseURL.absoluteString + path)
        }
        if !query.isEmpty {
            components.queryItems = query.map { URLQueryItem(name: $0.0, value: $0.1) }
        }
        guard let url = components.url else {
            throw APIError.invalidURL(components.description)
        }

        var request = URLRequest(url: url, timeoutInterval: 10)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await Self.session.data(for: request)
        } catch {
            throw APIError.transport(error.localizedDescription)
        }

        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            switch http.statusCode {
            case 401: throw APIError.unauthorized
            case 503: throw APIError.noData
            default: throw APIError.badStatus(http.statusCode)
            }
        }

        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw APIError.decoding(error.localizedDescription)
        }
    }
}
