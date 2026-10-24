import Foundation
import Observation

/// Stateless viewer: nothing is cached to disk, everything is re-fetched.
@MainActor
@Observable
final class AppState {
    private(set) var latest: LatestReading?
    private(set) var history: [HistoryPoint] = []
    private(set) var baseline: BaselineInfo?
    private(set) var consumption: [ConsumptionEntry] = []

    /// Erste Messsteckdose, falls es eine gibt. Ihre Kurve liegt *unter* der
    /// Gesamtkurve — der Abstand ist "alles außer diesem Gerät".
    private(set) var device: DeviceInfo?
    private(set) var deviceHistory: [HistoryPoint] = []

    private(set) var errorText: String?
    private(set) var isRefreshing = false
    private(set) var isLoadingDetail = false
    private(set) var lastUpdate: Date?
    private(set) var isConfigured: Bool

    var range: TimeRange = .day
    var consumptionPeriod = "day"

    /// Read once at launch, kept in memory.
    private var client: APIClient?

    /// Matches the Pi's live poll. The meter still only reports every ~2 min,
    /// so this buys reaction time, not resolution.
    private let refreshInterval: Duration = .seconds(30)

    init() {
        let restored = CredentialStore.read().flatMap { APIClient(credentials: $0) }
        client = restored
        isConfigured = restored != nil
        Task { await autoRefresh() }
    }

    var menuBarTitle: String {
        guard let power = latest?.powerW else { return "—" }
        return "\(Int(power.rounded())) W"
    }

    /// Cheap: only the current reading, on the 30 s timer.
    func refresh() async {
        guard let client, !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            latest = try await client.latest()
            lastUpdate = Date()
            errorText = nil
        } catch {
            latest = nil
            errorText = message(for: error)
        }
    }

    /// Expensive: history, baseline and consumption. Only while the popover is
    /// open — no point pulling a year of buckets every 30 seconds.
    func loadDetail() async {
        guard let client, !isLoadingDetail else { return }
        isLoadingDetail = true
        defer { isLoadingDetail = false }

        let to = Int(Date().timeIntervalSince1970)
        let from = to - range.seconds
        let count = consumptionPeriod == "day" ? 14 : 12
        do {
            async let historyTask = client.history(from: from, to: to)
            async let baselineTask = client.baseline(from: from, to: to)
            async let consumptionTask = client.consumption(period: consumptionPeriod,
                                                           count: count)
            history = try await historyTask
            baseline = try await baselineTask
            consumption = try await consumptionTask
            errorText = nil

            // Steckdosen sind optional: ohne eine gibt es einfach keine zweite
            // Linie, und ein Fehler hier darf die Hauptkurve nicht kippen.
            device = try await client.devices().first
            if let name = device?.device {
                deviceHistory = try await client.deviceHistory(device: name,
                                                               from: from, to: to)
            } else {
                deviceHistory = []
            }
        } catch {
            errorText = message(for: error)
        }
    }

    func save(baseURL: String, token: String) {
        let credentials = Credentials(baseURL: APIClient.normalized(baseURL), token: token)
        guard let newClient = APIClient(credentials: credentials) else {
            errorText = APIError.invalidURL(baseURL).errorDescription
            return
        }
        guard CredentialStore.write(credentials) else {
            errorText = "Konnte Zugangsdaten nicht speichern"
            return
        }
        client = newClient
        isConfigured = true
        Task {
            await refresh()
            await loadDetail()
        }
    }

    private func message(for error: Error) -> String {
        (error as? APIError)?.errorDescription ?? error.localizedDescription
    }

    private func autoRefresh() async {
        while !Task.isCancelled {
            if isConfigured {
                await refresh()
                // Auch den Verlauf: die Popover-Ansicht bleibt zwischen zwei
                // Öffnungen am Leben, ihr .task läuft also nur einmal. Ohne
                // das hier bliebe das Diagramm auf dem Stand vom ersten
                // Öffnen stehen, bis jemand "Aktualisieren" drückt.
                await loadDetail()
            }
            try? await Task.sleep(for: refreshInterval)
        }
    }
}
