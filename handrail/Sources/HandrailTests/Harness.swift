import Foundation

/// A test harness in thirty lines, because the alternative is a dependency.
public enum T {
    nonisolated(unsafe) static var passed = 0
    nonisolated(unsafe) static var failed: [String] = []

    public static func ok(_ label: String, _ condition: Bool) {
        if condition {
            passed += 1
            print("  PASS   \(label)")
        } else {
            failed.append(label)
            print("  FAIL   \(label)")
        }
    }

    /// Counts compare as often as strings do, and "5" != "5 " is not the
    /// failure anyone wants to read about.
    public static func equal(_ label: String, _ got: Int?, _ want: Int?) {
        equal(label, got.map(String.init), want.map(String.init))
    }

    public static func equal(_ label: String, _ got: String?, _ want: String?) {
        if got == want {
            passed += 1
            print("  PASS   \(label)")
        } else {
            failed.append(label)
            print("  FAIL   \(label)")
            print("           wanted: \(want ?? "nil")")
            print("           got:    \(got ?? "nil")")
        }
    }

    public static func section(_ name: String) { print("\n-- \(name) --") }

    public static func finish() -> Never {
        print("\n\(passed) passed, \(failed.count) failed")
        for f in failed { print("  FAILED: \(f)") }
        exit(failed.isEmpty ? 0 : 1)
    }
}
