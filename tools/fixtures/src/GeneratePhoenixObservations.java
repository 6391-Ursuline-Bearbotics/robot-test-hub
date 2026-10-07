import java.nio.file.Files;
import java.nio.file.Path;
import org.littletonrobotics.junction.LogTable;
import org.littletonrobotics.junction.wpilog.WPILOGWriter;
import org.littletonrobotics.junction.wpilog.WPILOGWriter.AdvantageScopeOpenBehavior;
import org.wpilib.util.WPIUtilJNI;

/** Invented SDK observations through the locked native writer. No CAN, HAL or robot startup. */
public final class GeneratePhoenixObservations {
  private static final long EPOCH_NS = 1_791_003_600_000_000_000L;

  public static void main(String[] args) throws Exception {
    Path out = Path.of(args[0]).toAbsolutePath();
    Files.createDirectories(out);
    WPIUtilJNI.enableMockTime();
    WPIUtilJNI.setMockTime(1_000_000_000L);
    try {
      write(out, "phoenix-warning", true, true);
      write(out, "phoenix-clear", true, false);
      write(out, "phoenix-sim-unavailable", false, false);
    } finally {
      WPIUtilJNI.disableMockTime();
    }
  }

  private static void write(Path out, String name, boolean present, boolean warning) {
    WPILOGWriter writer = new WPILOGWriter(out.resolve(name + ".wpilog").toString(),
        AdvantageScopeOpenBehavior.NEVER);
    writer.start();
    LogTable table = new LogTable(0);
    try {
      for (int sample = 0; sample < 8; sample++) {
        long stamp = 1_000_000_000L + sample * 100_000_000L;
        boolean enabled = sample > 0 && sample < 7;
        table.setTimestamp(stamp);
        table.put("Metadata/FixtureProfile", "wpilib-2027.0.0-alpha-7_akit-27.0.0-alpha-6");
        table.put("Metadata/SourceType", "SYNTHETIC");
        table.put("Metadata/RobotId", "native-phoenix-robot");
        table.put("Metadata/BootId", "invented-" + name);
        table.put("RealOutputs/TestHub/RuntimeMode", present ? "REAL" : "SIM");
        table.put("Fixture/Sequence", (long) sample);
        table.put("Fixture/Mode", enabled ? "TELEOP" : "DISABLED");
        table.put("RealOutputs/Drive/RequestedSpeed", enabled ? 1.0 : 0.0);
        table.put("DriverStation/Enabled", enabled);
        table.put("SystemStats/EpochTime", (EPOCH_NS + stamp) / 1000.0, "microseconds");
        table.put("SystemStats/EpochTimeValid", true);
        for (int module = 0; module < 4; module++) {
          LogTable inputs = table.getSubtable("Drive/Module" + module);
          // A short SDK failure may precede the existing falling connection debounce.
          boolean statusOk = !(warning && module == 0 && sample >= 2 && sample <= 4);
          inputs.put("DriveConnected", true);
          inputs.put("TurnConnected", true);
          inputs.put("TurnEncoderConnected", true);
          inputs.put("PhoenixDiagnosticsPresent", present);
          inputs.put("PhoenixDiagnosticsProfile", present ? "phoenix6-status-observation-1" : "unavailable");
          inputs.put("PhoenixSnapshotMethod", present ? "scheduler_owned_cached_read_after_existing_refresh" : "unavailable");
          inputs.put("PhoenixObservationSequence", present ? (long) sample + 1 : 0L);
          inputs.put("PhoenixRobotObservationStartNs", present ? stamp - 1000 : 0L);
          inputs.put("PhoenixRobotObservationEndNs", present ? stamp + 1000 : 0L);
          double vendorStart = 20.0 + sample * .1;
          inputs.put("PhoenixVendorObservationStartSeconds", present ? vendorStart : Double.NaN);
          inputs.put("PhoenixVendorObservationEndSeconds", present ? vendorStart + .001 : Double.NaN);
          inputs.put("PhoenixObservationClockValid", present);
          inputs.put("PhoenixObservationClockRegressed", false);
          inputs.put("PhoenixPhysicalAcquisitionTimeQualified", false);
          inputs.put("PhoenixNativeTimestampAvailabilityQualified", false);
          inputs.put("PhoenixDriveGroupRefreshStatusCode", statusOk ? 0 : -1);
          inputs.put("PhoenixDriveGroupRefreshStatusOk", present && statusOk);
          inputs.put("PhoenixTurnGroupRefreshStatusCode", 0);
          inputs.put("PhoenixTurnGroupRefreshStatusOk", present);
          // A held receipt on sample 3, with equal values throughout, is diagnostic only.
          double receipt = 20.0 + (sample == 3 ? 2 : sample) * .1;
          signal(inputs, "PhoenixDriveVelocity", present, statusOk, 2.5, receipt,
              vendorStart, sample);
          signal(inputs, "PhoenixTurnPosition", present, true, .125, receipt,
              vendorStart, sample);
        }
        writer.putTable(LogTable.clone(table));
      }
    } finally {
      writer.end();
    }
  }

  private static void signal(LogTable table, String prefix, boolean present,
      boolean statusOk, double raw, double receipt, double vendorStart, int sample) {
    table.put(prefix + "RawValue", present ? raw : Double.NaN);
    table.put(prefix + "StatusCode", statusOk ? 0 : -1);
    table.put(prefix + "StatusOk", present && statusOk);
    table.put(prefix + "BestTimestampSeconds", present ? receipt - .012 : Double.NaN);
    table.put(prefix + "BestTimestampSource", present ? 1 : -1);
    table.put(prefix + "BestTimestampValid", present);
    table.put(prefix + "SystemTimestampSeconds", present ? receipt - .008 : Double.NaN);
    table.put(prefix + "SystemTimestampValid", present);
    table.put(prefix + "CANivoreTimestampSeconds", present ? receipt - .012 : Double.NaN);
    table.put(prefix + "CANivoreTimestampValid", present);
    table.put(prefix + "DeviceTimestampSeconds", present ? 0.0 : Double.NaN);
    table.put(prefix + "DeviceTimestampValid", false);
    table.put(prefix + "ReceiptComparison", !present ? "unknown" : sample == 0 ? "first" : sample == 3 ? "held" : "advanced");
    table.put(prefix + "RawValueComparison", !present ? "unknown" : sample == 0 ? "first" : "held");
    table.put(prefix + "BestTimestampSourceChanged", false);
    table.put(prefix + "AgeAtObservationStartSeconds", present ? vendorStart - (receipt - .012) : Double.NaN);
    table.put(prefix + "AgeAtObservationEndSeconds", present ? vendorStart + .001 - (receipt - .012) : Double.NaN);
    table.put(prefix + "TimestampInFuture", false);
  }
}
