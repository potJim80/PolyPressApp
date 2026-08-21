import Foundation

/// An RStudio project on disk.
///
/// Following the .Rproj is what makes "it just appears in his script" true: the
/// project is the thing he already opens, so the app and RStudio are pointed at
/// the same place without either being told twice.
public struct RProject: Sendable, Equatable {
    public let folder: URL
    public let projectFile: URL?

    public var name: String {
        projectFile?.deletingPathExtension().lastPathComponent ?? folder.lastPathComponent
    }

    public init(folder: URL) {
        self.folder = folder
        self.projectFile = RProject.findProjectFile(in: folder)
    }

    /// The project folder for whatever was picked.
    ///
    /// The thing on screen that says "this is the project" is the .Rproj file,
    /// so that is what people reach for — but a project is a folder. Both
    /// answers land in the same place rather than one of them being wrong.
    public static func folder(for url: URL) -> URL {
        var isDir: ObjCBool = false
        if FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir),
           isDir.boolValue { return url }
        return url.deletingLastPathComponent()
    }

    /// Whether picking this in a file panel should be allowed: folders, and the
    /// .Rproj itself. Anything else would be a guess about which folder was meant.
    public static func isPickable(_ url: URL) -> Bool {
        var isDir: ObjCBool = false
        if FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir),
           isDir.boolValue { return true }
        return url.pathExtension.lowercased() == "rproj"
    }

    /// The .Rproj in a folder, if there is one. A folder without one still works
    /// -- plenty of people never make a project -- it just does not get the name.
    public static func findProjectFile(in folder: URL) -> URL? {
        let items = (try? FileManager.default.contentsOfDirectory(
            at: folder, includingPropertiesForKeys: nil)) ?? []
        return items.first { $0.pathExtension.lowercased() == "rproj" }
    }

    /// The R scripts in the project, nearest the top first. Only one level down
    /// as well as the top: deeper than that and the list stops being a list.
    public func scripts() -> [URL] {
        let fm = FileManager.default
        var found: [URL] = []
        let top = (try? fm.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil,
                                               options: [.skipsHiddenFiles])) ?? []
        for item in top {
            if item.pathExtension.lowercased() == "r" { found.append(item) }
            var isDir: ObjCBool = false
            if fm.fileExists(atPath: item.path, isDirectory: &isDir), isDir.boolValue,
               !["renv", "packrat", ".git"].contains(item.lastPathComponent) {
                let inner = (try? fm.contentsOfDirectory(at: item, includingPropertiesForKeys: nil,
                                                         options: [.skipsHiddenFiles])) ?? []
                found += inner.filter { $0.pathExtension.lowercased() == "r" }
            }
        }
        return found.sorted { $0.lastPathComponent.lowercased() < $1.lastPathComponent.lowercased() }
    }

    /// The data files worth offering, so the file picker is a last resort rather
    /// than the first step.
    public func dataFiles() -> [URL] {
        let fm = FileManager.default
        let wanted = Set(["csv", "tsv", "txt"])
        var found: [URL] = []
        for dir in [folder, folder.appendingPathComponent("data"),
                    folder.appendingPathComponent("Data")] {
            let items = (try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil,
                                                     options: [.skipsHiddenFiles])) ?? []
            found += items.filter { wanted.contains($0.pathExtension.lowercased()) }
        }
        return found.sorted { $0.lastPathComponent.lowercased() < $1.lastPathComponent.lowercased() }
    }

    /// The path to write into the script. A file inside the project is written
    /// relative to it, because that is what makes the script still run on
    /// another machine — which is the whole reason for handing over a script.
    public func referenceTo(_ file: URL) -> String {
        let base = folder.standardizedFileURL.path
        let target = file.standardizedFileURL.path
        if target.hasPrefix(base + "/") {
            return String(target.dropFirst(base.count + 1))
        }
        return target
    }
}
