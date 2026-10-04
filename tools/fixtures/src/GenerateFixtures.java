import java.nio.file.Files;
import java.nio.file.Path;
import org.littletonrobotics.junction.LogTable;
import org.littletonrobotics.junction.wpilog.WPILOGWriter;
import org.littletonrobotics.junction.wpilog.WPILOGWriter.AdvantageScopeOpenBehavior;
import org.wpilib.math.geometry.Pose2d;
import org.wpilib.math.geometry.Rotation2d;
import org.wpilib.util.WPIUtilJNI;

/** Synthetic data only. Never starts Logger, HAL, a robot, or a network client. */
public final class GenerateFixtures {
  public static void main(String[] args) throws Exception {
    Path out = Path.of(args[0]).toAbsolutePath();
    Files.createDirectories(out);
    // AK starts /Timestamp with time zero (meaning now). Freeze that control record as well.
    WPIUtilJNI.enableMockTime();
    WPIUtilJNI.setMockTime(1_000_000_000L);
    try {
      write(out, "alpha7-main", "synthetic-boot-a",
          new long[] {1_000_000_000L, 1_020_000_123L, 1_040_000_000L, 1_060_000_000L,
              1_080_000_000L, 1_500_000_000L, 1_520_000_000L},
          new double[] {0, 1_791_003_600_020_000d, 1_791_003_600_040_000d,
              1_791_003_602_060_000d, 0, 1_791_003_602_500_000d, 1_791_003_602_520_000d},
          new boolean[] {false, true, true, true, false, true, true},
          new String[] {"DISABLED", "AUTONOMOUS", "AUTONOMOUS", "TELEOP", "DISABLED", "TELEOP", "DISABLED"}, true);
      write(out, "alpha7-boot-b", "synthetic-boot-b",
          new long[] {1_000_000_000L, 1_020_000_000L, 1_040_000_000L},
          new double[] {1_791_003_605_000_000d, 1_791_003_605_020_000d, 1_791_003_605_040_000d},
          new boolean[] {true, true, true}, new String[] {"DISABLED", "TELEOP", "TELEOP"}, false);
      // 2026-11-01 01:30 in America/Chicago occurs twice, one hour apart in UTC.
      write(out, "alpha7-dst-overlap", "synthetic-boot-fall",
          new long[] {1_000_000_000L, 3_601_000_000_000L},
          new double[] {1_793_514_600_000_000d, 1_793_518_200_000_000d},
          new boolean[] {true, true}, new String[] {"DISABLED", "DISABLED"}, true);
      // Spring clocks jump directly from 01:59:59 CST to 03:00:00 CDT.
      write(out, "alpha7-dst-gap", "synthetic-boot-spring",
          new long[] {1_000_000_000L, 2_000_000_000L},
          new double[] {1_772_956_799_000_000d, 1_772_956_800_000_000d},
          new boolean[] {true, true}, new String[] {"DISABLED", "DISABLED"}, true);
    } finally {
      WPIUtilJNI.disableMockTime();
    }
  }

  private static void write(Path out, String name, String boot, long[] times, double[] epochs,
      boolean[] valid, String[] modes, boolean complete) {
    WPILOGWriter writer = new WPILOGWriter(out.resolve(name + ".wpilog").toString(), AdvantageScopeOpenBehavior.NEVER);
    writer.start();
    LogTable table = new LogTable(0);
    try {
      for (int i = 0; i < times.length; i++) {
        table.setTimestamp(times[i]);
        table.put("Metadata/FixtureProfile", "wpilib-2027.0.0-alpha-7_akit-27.0.0-alpha-6");
        table.put("Metadata/SourceType", "SYNTHETIC");
        table.put("Metadata/RobotId", "synthetic-team-6391");
        table.put("Metadata/BootId", boot);
        table.put("Metadata/BuildId", "fixture-source-v1");
        table.put("Fixture/Sequence", (long) i);
        table.put("Fixture/Mode", modes[i]);
        table.put("DriverStation/Enabled", !modes[i].equals("DISABLED"));
        table.put("DriverStation/Autonomous", modes[i].equals("AUTONOMOUS"));
        table.put("SystemStats/EpochTime", epochs[i], "microseconds");
        table.put("SystemStats/EpochTimeValid", valid[i]);
        table.put("RealOutputs/Drive/RequestedSpeed", 1.0 + i, "meters per second");
        table.put("ReplayOutputs/Drive/RequestedSpeed", 10.0 + i, "meters per second");
        table.put("Drive/Module0/Connected", i != 3);
        if (i != 3) { // Missing array sample: retained state is not a fresh observation.
          table.put("Drive/Module0/OdometryTimestamps", new double[] {times[i] / 1e9 - .005, times[i] / 1e9});
          table.put("Drive/Module0/OdometryDrivePositionsRad", new double[] {i + .25, i + .5});
        }
        table.put("Fixture/NestedArray", new double[][] {{i + .5, -i - .5}, {}, {i + 2.0}});
        Pose2d pose = new Pose2d(1.25 + i, -2.5, Rotation2d.fromRadians(.25));
        table.put("Drive/Pose", Pose2d.struct, pose);
        table.put("Drive/PoseArray", Pose2d.struct, new Pose2d[] {pose, new Pose2d(-1, 2, Rotation2d.fromRadians(-.5))});
        table.put("Fixture/UnitsChange", i == 0 ? 1.0 : 100.0 + i, i == 0 ? "meters" : "centimeters");
        // Every writer closes cleanly; this flag represents whether a robot terminal event exists.
        if (i == times.length - 1 && complete) table.put("Fixture/TerminalEvent", "NORMAL_END");
        writer.putTable(LogTable.clone(table));
      }
    } finally {
      writer.end();
    }
  }
}
