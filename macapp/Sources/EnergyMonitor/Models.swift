import Foundation

/// `GET /latest` — newest power reading plus the newest counter.
struct LatestReading: Decodable, Sendable {
    let ts: Int
    let powerW: Double
    let live: Bool?
    let energyWh: Double?
    let energyTs: Int?

    enum CodingKeys: String, CodingKey {
        case ts, live
        case powerW = "power_w"
        case energyWh = "energy_wh"
        case energyTs = "energy_ts"
    }

    var date: Date { Date(timeIntervalSince1970: TimeInterval(ts)) }
    var energyKWh: Double? { energyWh.map { $0 / 1000 } }
}

/// `GET /history` — one entry per server-side bucket.
struct HistoryPoint: Decodable, Sendable, Identifiable {
    let ts: Int
    let powerAvg: Double
    let powerMin: Double
    let powerMax: Double
    let samples: Int

    enum CodingKeys: String, CodingKey {
        case ts, samples
        case powerAvg = "power_avg"
        case powerMin = "power_min"
        case powerMax = "power_max"
    }

    var id: Int { ts }
    var date: Date { Date(timeIntervalSince1970: TimeInterval(ts)) }
}

/// `GET /devices` — one entry per measuring plug.
struct DeviceInfo: Decodable, Sendable, Identifiable {
    let device: String
    let samples: Int
    let powerW: Double?
    let energyWh: Double?
    let lastError: String?

    enum CodingKeys: String, CodingKey {
        case device, samples
        case powerW = "power_w"
        case energyWh = "energy_wh"
        case lastError = "last_error"
    }

    var id: String { device }
}

/// `GET /consumption` — kWh per day or month, from counter deltas.
/// `kwh` is null until a counter reading exists from before the period began.
struct ConsumptionEntry: Decodable, Sendable, Identifiable {
    let period: String
    let startTs: Int
    let start: String
    let kwh: Double?
    let counterWh: Double?

    enum CodingKeys: String, CodingKey {
        case period, start, kwh
        case startTs = "start_ts"
        case counterWh = "counter_wh"
    }

    var id: Int { startTs }
    var date: Date { Date(timeIntervalSince1970: TimeInterval(startTs)) }
}

/// `GET /baseline` — the standing load in a window.
struct BaselineInfo: Decodable, Sendable {
    let samples: Int
    let minW: Double?
    let p05W: Double?
    let avgW: Double?

    enum CodingKeys: String, CodingKey {
        case samples
        case minW = "min_w"
        case p05W = "p05_w"
        case avgW = "avg_w"
    }
}

enum TimeRange: String, CaseIterable, Identifiable, Sendable {
    case hour = "1h", day = "24h", week = "7d", month = "30d", year = "1y"

    var id: String { rawValue }

    var seconds: Int {
        switch self {
        case .hour: return 3600
        case .day: return 86400
        case .week: return 7 * 86400
        case .month: return 30 * 86400
        case .year: return 365 * 86400
        }
    }
}

enum APIError: LocalizedError, Sendable {
    case notConfigured
    case invalidURL(String)
    case unauthorized
    case noData
    case badStatus(Int)
    case transport(String)
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .notConfigured: return "Pi-Adresse und Token fehlen"
        case .invalidURL(let text): return "Ungültige Adresse: \(text)"
        case .unauthorized: return "Token abgelehnt (401)"
        case .noData: return "Noch keine Messwerte auf dem Pi"
        case .badStatus(let code): return "Pi antwortet mit HTTP \(code)"
        case .transport(let message): return "Pi nicht erreichbar: \(message)"
        case .decoding(let message): return "Antwort unlesbar: \(message)"
        }
    }
}
