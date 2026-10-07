import java.nio.file.Files;
import java.nio.file.Path;
import org.littletonrobotics.junction.LogTable;
import org.littletonrobotics.junction.wpilog.WPILOGWriter;
import org.littletonrobotics.junction.wpilog.WPILOGWriter.AdvantageScopeOpenBehavior;
import org.wpilib.math.geometry.Rotation2d;
import org.wpilib.math.kinematics.SwerveModuleVelocity;
import org.wpilib.util.WPIUtilJNI;

/** Invented observations only: no physics, HAL, robot, camera or network operation. */
public final class GenerateSwerveSessions {
  private static final long EPOCH_NS = 1_791_003_600_000_000_000L;

  public static void main(String[] args) throws Exception {
    Path out = Path.of(args[0]).toAbsolutePath();
    Files.createDirectories(out);
    WPIUtilJNI.enableMockTime();
    WPIUtilJNI.setMockTime(1_000_000_000L);
    try {
      write(out, "swerve-sim", "native-swerve-boot-a", "SIM", 0,
          new double[] {0, .02, .04, .35});
      write(out, "swerve-sim-later", "native-swerve-boot-b", "SIM", 30_000_000_000L,
          new double[] {.35});
      write(out, "swerve-real-mode", "native-swerve-boot-c", "REAL", 60_000_000_000L,
          new double[] {.35});
    } finally {
      WPIUtilJNI.disableMockTime();
    }
  }

  private static void write(Path out, String name, String boot, String runtime,
      long epochOffset, double[] errors) {
    WPILOGWriter writer = new WPILOGWriter(out.resolve(name + ".wpilog").toString(),
        AdvantageScopeOpenBehavior.NEVER);
    writer.start();
    LogTable table = new LogTable(0);
    long sequence = 0;
    try {
      for (int run = 0; run < errors.length; run++) {
        // Explicit disabled boundaries and a 100 ms cycle clock throughout.
        for (int sample = 0; sample <= 21; sample++, sequence++) {
          long stamp = 1_000_000_000L + sequence * 100_000_000L;
          boolean enabled = sample > 0 && sample < 21;
          table.setTimestamp(stamp);
          table.put("Metadata/FixtureProfile", "wpilib-2027.0.0-alpha-7_akit-27.0.0-alpha-6");
          table.put("Metadata/SourceType", "SYNTHETIC");
          table.put("Metadata/RobotId", "native-swerve-robot");
          table.put("Metadata/BootId", boot);
          table.put("RealMetadata/SourceSHA256", "a".repeat(64));
          table.put("RealOutputs/TestHub/ConfigurationSHA256", "b".repeat(64));
          table.put("RealOutputs/TestHub/RuntimeMode", runtime);
          table.put("Fixture/Sequence", sequence);
          table.put("Fixture/Mode", enabled ? "TELEOP" : "DISABLED");
          table.put("DriverStation/Enabled", enabled);
          table.put("SystemStats/EpochTime", (EPOCH_NS + epochOffset + stamp) / 1000.0,
              "microseconds");
          table.put("SystemStats/EpochTimeValid", true);
          table.put("RealOutputs/Drive/RequestedSpeed", enabled ? 1.0 : 0.0);
          SwerveModuleVelocity[] commands = new SwerveModuleVelocity[4];
          SwerveModuleVelocity[] measured = new SwerveModuleVelocity[4];
          for (int module = 0; module < 4; module++) {
            commands[module] = new SwerveModuleVelocity(enabled ? 1.0 : 0.0,
                Rotation2d.ZERO);
            measured[module] = new SwerveModuleVelocity(enabled ?
                1.0 - (module == 0 ? errors[run] : 0) : 0.0, Rotation2d.ZERO);
            table.put("Drive/Module" + module + "/DriveConnected", true);
            table.put("Drive/Module" + module + "/TurnConnected", true);
            table.put("Drive/Module" + module + "/TurnEncoderConnected", true);
          }
          table.put("RealOutputs/SwerveStates/SetpointsOptimized",
              SwerveModuleVelocity.struct, commands);
          table.put("RealOutputs/SwerveStates/Measured", SwerveModuleVelocity.struct, measured);
          writer.putTable(LogTable.clone(table));
        }
      }
    } finally {
      writer.end();
    }
  }
}
