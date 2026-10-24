import Charts
import SwiftUI

/// Schrittweite der Y-Achsen. Sie wachsen mit den Daten, aber nur in diesen
/// Sprüngen — eine stufenlos mitwandernde Achse macht jeden neuen Messwert zu
/// einem Sprung im Bild und Kurven zwischen zwei Blicken unvergleichbar.
/// Getrennt je Diagramm, weil der Hauszähler in Kilowatt denkt und eine
/// Messsteckdose in zweistelligen Watt.
private let totalScaleStepW = 250.0
private let deviceScaleStepW = 100.0

/// Obergrenze für eine Reihe: der höchste Wert, aufgerundet auf den nächsten
/// Schritt. `powerMax` statt `powerAvg`, damit auch das Min/Max-Band passt.
private func scaleMax(_ points: [HistoryPoint], step: Double) -> Double {
    let peak = points.map(\.powerMax).max() ?? 0
    return max(step, (peak / step).rounded(.up) * step)
}

/// Zeitachse mit einer Ebene: nur die Uhrzeit, Teilung passend zum gewählten
/// Bereich. Eine zweite Ebene mit dem Datum war ausprobiert und wieder
/// verworfen — sie brachte im Alltag nichts, weil ohnehin klar ist, welcher
/// Tag gemeint ist.
private struct TimeAxis: ViewModifier {
    let range: TimeRange

    func body(content: Content) -> some View {
        content.chartXAxis {
            AxisMarks(values: .stride(by: fine.unit, count: fine.count)) { _ in
                AxisGridLine()
                AxisTick()
                AxisValueLabel(format: fineFormat)
                    .font(.caption2)
            }
        }
    }

    private var fine: (unit: Calendar.Component, count: Int) {
        switch range {
        case .hour: return (.minute, 15)
        case .day: return (.hour, 4)
        case .week: return (.day, 1)
        case .month: return (.day, 5)
        case .year: return (.month, 1)
        }
    }

    private var fineFormat: Date.FormatStyle {
        switch range {
        case .hour, .day: return .dateTime.hour().minute()
        case .week, .month: return .dateTime.day().month(.abbreviated)
        case .year: return .dateTime.month(.abbreviated)
        }
    }
}

/// Überschrift eines Diagramms: farbiger Punkt, Name, aktueller Wert. Der
/// Abstand nach unten ist bewusst großzügig — direkt an der Zeichenfläche
/// wirkt die Zeile wie ein Teil des Diagramms.
private struct ChartCaption: View {
    let color: Color
    let title: String
    let watts: Double?

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(title)
            if let watts {
                Text("\(Int(watts.rounded())) W")
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
        }
        .font(.caption)
    }
}

struct MenuBarContentView: View {
    @Bindable var state: AppState
    @State private var tab = Tab.power

    enum Tab: String, CaseIterable, Identifiable {
        case power = "Leistung", energy = "Energie"
        var id: String { rawValue }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if state.isConfigured {
                CurrentReadingView(state: state)
                Picker("", selection: $tab) {
                    ForEach(Tab.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()

                switch tab {
                case .power: PowerView(state: state)
                case .energy: EnergyView(state: state)
                }
            } else {
                SetupView(state: state)
            }

            Divider()
            FooterView(state: state)
        }
        .padding(14)
        .frame(width: 380)
        .task { await state.loadDetail() }
    }
}

// MARK: - current reading

private struct CurrentReadingView: View {
    let state: AppState

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(state.latest.map { "\(Int($0.powerW.rounded())) W" } ?? "—")
                .font(.system(size: 34, weight: .semibold, design: .rounded))
                .monospacedDigit()
            Spacer()
            if let kWh = state.latest?.energyKWh {
                VStack(alignment: .trailing, spacing: 2) {
                    Text(String(format: "%.1f kWh", kWh))
                        .font(.callout).monospacedDigit()
                    Text("Zählerstand").font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
    }
}

// MARK: - power over time

private struct PowerView: View {
    @Bindable var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Picker("", selection: $state.range) {
                ForEach(TimeRange.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .onChange(of: state.range) { Task { await state.loadDetail() } }

            if state.history.isEmpty {
                PlaceholderView(isLoading: state.isLoadingDetail,
                                empty: "Für diesen Zeitraum gibt es noch keine Daten")
            } else {
                totalChart
                if !state.deviceHistory.isEmpty {
                    deviceChart
                }
            }

            BaselineRow(baseline: state.baseline)
        }
    }

    private var totalChart: some View {
        VStack(alignment: .leading, spacing: 6) {
            ChartCaption(color: .blue, title: "Wohnungszähler",
                         watts: state.latest?.powerW)
            totalPlot
        }
    }

    private var totalPlot: some View {
        Chart {
            // The band shows what averaging hides: brief peaks inside a
            // bucket. Without it a 2000 W kettle vanishes at 30 days.
            ForEach(state.history) { point in
                AreaMark(x: .value("Zeit", point.date),
                         yStart: .value("min", point.powerMin),
                         yEnd: .value("max", point.powerMax))
                    .foregroundStyle(.blue.opacity(0.12))
            }
            ForEach(state.history) { point in
                LineMark(x: .value("Zeit", point.date),
                         y: .value("Watt", point.powerAvg))
                    .foregroundStyle(.blue)
                    .interpolationMethod(.monotone)
            }
        }
        .chartYScale(domain: 0...scaleMax(state.history, step: totalScaleStepW))
        .chartXScale(domain: xDomain)
        .chartYAxis { AxisMarks(position: .leading) }
        .modifier(TimeAxis(range: state.range))
        .frame(height: 140)
    }

    private var deviceChart: some View {
        VStack(alignment: .leading, spacing: 6) {
            ChartCaption(color: .orange,
                         title: state.device?.device ?? "Gerät",
                         watts: state.device?.powerW)

            Chart(state.deviceHistory) { point in
                AreaMark(x: .value("Zeit", point.date),
                         yStart: .value("min", point.powerMin),
                         yEnd: .value("max", point.powerMax))
                    .foregroundStyle(.orange.opacity(0.12))
                LineMark(x: .value("Zeit", point.date),
                         y: .value("Watt", point.powerAvg))
                    .foregroundStyle(.orange)
                    .interpolationMethod(.monotone)
            }
            .chartYScale(domain: 0...scaleMax(state.deviceHistory, step: deviceScaleStepW))
            .chartXScale(domain: xDomain)
            .chartYAxis { AxisMarks(position: .leading) }
            .modifier(TimeAxis(range: state.range))
            .frame(height: 100)
        }
    }

    /// Beide Diagramme teilen sich die Zeitachse. Die Spanne kommt aus der
    /// Vereinigung beider Reihen, nicht aus dem angefragten Fenster — sonst
    /// stünde links ein leerer Bereich, solange die Historie kürzer ist.
    private var xDomain: ClosedRange<Date> {
        let dates = state.history.map(\.date) + state.deviceHistory.map(\.date)
        guard let first = dates.min(), let last = dates.max(), first < last else {
            let now = Date()
            return now.addingTimeInterval(-3600)...now
        }
        return first...last
    }
}

/// The standing-load view: what the household draws when nothing is running.
private struct BaselineRow: View {
    let baseline: BaselineInfo?

    var body: some View {
        HStack(spacing: 14) {
            Label("Grundlast", systemImage: "arrow.down.to.line")
                .font(.caption).foregroundStyle(.secondary)
            if let baseline, let min = baseline.minW, let p05 = baseline.p05W {
                Text("min \(Int(min.rounded())) W")
                Text("5 % \(Int(p05.rounded())) W")
                if let avg = baseline.avgW {
                    Text("Ø \(Int(avg.rounded())) W").foregroundStyle(.secondary)
                }
            } else {
                Text("—").foregroundStyle(.secondary)
            }
        }
        .font(.caption).monospacedDigit()
    }
}

// MARK: - energy per day / month

private struct EnergyView: View {
    @Bindable var state: AppState

    private var known: [ConsumptionEntry] { state.consumption.filter { $0.kwh != nil } }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Picker("", selection: $state.consumptionPeriod) {
                Text("Tage").tag("day")
                Text("Monate").tag("month")
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .onChange(of: state.consumptionPeriod) { Task { await state.loadDetail() } }

            if known.isEmpty {
                // Not an error: a period needs a counter reading from before it
                // started, so the first full day only appears after midnight.
                PlaceholderView(
                    isLoading: state.isLoadingDetail,
                    empty: "Noch kein abgeschlossener Zeitraum — Tageswerte gibt es "
                         + "ab dem ersten Mitternachtswechsel, Monatswerte ab dem 1.")
            } else {
                Chart(known) { entry in
                    BarMark(x: .value("Zeitraum", entry.date, unit: unit),
                            y: .value("kWh", entry.kwh ?? 0))
                        .foregroundStyle(.green.gradient)
                }
                .chartYAxis { AxisMarks(position: .leading) }
                .frame(height: 150)

                if let last = known.last, let kwh = last.kwh {
                    Text(String(format: "%@: %.2f kWh", last.start, kwh))
                        .font(.caption).monospacedDigit().foregroundStyle(.secondary)
                }
            }
        }
    }

    private var unit: Calendar.Component {
        state.consumptionPeriod == "day" ? .day : .month
    }
}

// MARK: - shared bits

private struct PlaceholderView: View {
    let isLoading: Bool
    let empty: String

    var body: some View {
        HStack {
            Spacer()
            if isLoading {
                ProgressView().controlSize(.small)
            } else {
                Text(empty)
                    .font(.caption).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            Spacer()
        }
        .frame(height: 150)
    }
}

private struct FooterView: View {
    let state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let errorText = state.errorText {
                Label(errorText, systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                if let measured = state.latest?.date {
                    Text("Messung \(measured.formatted(date: .omitted, time: .shortened))")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    Task {
                        await state.refresh()
                        await state.loadDetail()
                    }
                } label: {
                    if state.isRefreshing || state.isLoadingDetail {
                        ProgressView().controlSize(.small)
                    } else {
                        Text("Aktualisieren")
                    }
                }
                .disabled(!state.isConfigured)
                Button("Beenden") { NSApplication.shared.terminate(nil) }
            }
            .buttonStyle(.borderless)
            .font(.callout)
        }
    }
}

/// Minimal first-run entry. The proper Settings view comes in Phase 6.
private struct SetupView: View {
    let state: AppState
    @State private var baseURL = "raspberrypi.local:8000"
    @State private var token = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Pi verbinden").font(.headline)
            TextField("Adresse", text: $baseURL)
            SecureField("API-Token", text: $token)
            Button("Sichern") { state.save(baseURL: baseURL, token: token) }
                .disabled(baseURL.isEmpty || token.isEmpty)
        }
        .textFieldStyle(.roundedBorder)
    }
}
