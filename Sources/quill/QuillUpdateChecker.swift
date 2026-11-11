import Foundation

/// Checks the versioned source published on this fork's main branch. Each link
/// refers to the same commit as the Info.plist used for the comparison.
enum QuillUpdateChecker {
    struct Candidate: Equatable, Sendable {
        let commit: String
        let version: String
        let build: String
    }

    enum Result: Equatable, Sendable {
        case current(version: String)
        case localNewer(installed: String, published: String)
        case available(installed: String, published: String, source: URL, guide: URL, restart: Candidate? = nil)
    }

    enum CheckError: LocalizedError {
        case unknownInstalledVersion, unreadableVersion, rateLimited, unavailable, notFound

        var errorDescription: String? {
            switch self {
            case .unknownInstalledVersion: "Quill couldn’t read the version of this installation."
            case .unreadableVersion: "Quill couldn’t verify the version published on GitHub. Try again later."
            case .rateLimited: "GitHub is limiting update checks. Try again later."
            case .unavailable, .notFound: "Quill couldn’t reach the update information on GitHub. Check your connection and try again."
            }
        }
    }

    private struct Version {
        let label: String
        let numbers: [Int]

        init?(_ version: String?, build: String?) {
            guard let version, let build else { return nil }
            let parts = version.split(separator: ".", omittingEmptySubsequences: false)
            guard parts.count == 3 else { return nil }
            // Match the four-digit component limits used by the app packager.
            let fields = parts + [build[...]]
            let numbers = fields.compactMap { part -> Int? in
                guard (1...4).contains(part.utf8.count), part.utf8.allSatisfy({ (48...57).contains($0) }),
                      part.count == 1 || part.first != "0" else { return nil }
                return Int(part)
            }
            guard numbers.count == 4, numbers[3] > 0 else { return nil }
            self.numbers = numbers
            self.label = "\(version) (\(build))"
        }
    }

    private struct Branch: Decodable {
        struct Object: Decodable {
            let type: String
            let sha: String
        }
        let ref: String
        let object: Object
    }

    private struct AppInfo: Decodable {
        let CFBundleIdentifier: String
        let CFBundleShortVersionString: String
        let CFBundleVersion: String
    }

    private struct Release: Decodable {
        let tag_name: String
        let draft: Bool
        let prerelease: Bool
        let assets: [Asset]

        struct Asset: Decodable {
            let name: String
            let state: String
            let size: Int
            let digest: String?
            let browser_download_url: URL
        }
    }

    #if arch(arm64)
    static let assetName = "quill-macos-arm64"
    #else
    static let assetName = "quill-macos-x86_64"
    #endif

    private static let repository = URL(string: "https://github.com/Abraze101/quill")!
    private static let api = URL(string: "https://api.github.com/repos/Abraze101/quill")!

    static func check() async throws -> Result {
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.urlCredentialStorage = nil
        config.urlCache = nil
        config.timeoutIntervalForRequest = 10
        config.timeoutIntervalForResource = 15
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        return try await check(
            installedVersion: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String,
            build: Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String,
            session: session,
            supportsRestart: Bundle.main.bundleURL.path == "/Applications/Quill.app"
                && Bundle.main.url(forResource: "app-update", withExtension: "py") != nil
        )
    }

    static func check(installedVersion: String?, build: String?, session: URLSession,
                      supportsRestart: Bool = true) async throws -> Result {
        guard let installed = Version(installedVersion, build: build) else { throw CheckError.unknownInstalledVersion }
        let branchData = try await read(api.appending(path: "git/ref/heads/main"), session: session)
        guard let branch = try? JSONDecoder().decode(Branch.self, from: branchData),
              branch.ref == "refs/heads/main", branch.object.type == "commit" else { throw CheckError.unreadableVersion }
        let sha = branch.object.sha
        guard isHex(sha, count: 40) else { throw CheckError.unreadableVersion }
        let (_, published) = try await version(at: sha, session: session)
        if installed.numbers == published.numbers { return .current(version: installed.label) }
        if published.numbers.lexicographicallyPrecedes(installed.numbers) {
            return .localNewer(installed: installed.label, published: published.label)
        }
        if supportsRestart {
            do {
                if let update = try await releasedUpdate(after: installed, session: session) { return update }
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                // The pinned source revision is verified. Keep it available if
                // GitHub can't confirm a compatible prebuilt release.
            }
        }
        return available(installed: installed, published: published, commit: sha)
    }

    private static func releasedUpdate(after installed: Version, session: URLSession) async throws -> Result? {
        // A release may still be the newest downloadable build after main advances.
        let data: Data
        do { data = try await read(api.appending(path: "releases/latest"), session: session) }
        catch CheckError.notFound { return nil }
        let release = try JSONDecoder().decode(Release.self, from: data)
        let tag = release.tag_name
        guard !release.draft, !release.prerelease, tag.first == "v",
              Version(String(tag.dropFirst()), build: "1") != nil else { return nil }
        let assets = release.assets.filter { $0.name == assetName }
        guard assets.count == 1, let asset = assets.first, asset.state == "uploaded",
              (1...(128 * 1024 * 1024)).contains(asset.size),
              let digest = asset.digest, digest.hasPrefix("sha256:"),
              isHex(String(digest.dropFirst(7)), count: 64),
              asset.browser_download_url == repository.appending(path: "releases/download/\(tag)/\(assetName)") else { return nil }
        let rawSHA = try await read(api.appending(path: "commits/\(tag)"), session: session, accept: "application/vnd.github.sha")
        let sha = String(decoding: rawSHA, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        guard isHex(sha, count: 40) else { throw CheckError.unreadableVersion }
        let (info, published) = try await version(at: sha, session: session)
        guard tag == "v\(info.CFBundleShortVersionString)" else { throw CheckError.unreadableVersion }
        guard installed.numbers.lexicographicallyPrecedes(published.numbers) else { return nil }
        let candidate = Candidate(commit: sha, version: info.CFBundleShortVersionString, build: info.CFBundleVersion)
        return available(installed: installed, published: published, commit: sha, restart: candidate)
    }

    private static func version(at sha: String, session: URLSession) async throws -> (AppInfo, Version) {
        let plist = api.appending(path: "contents/Sources/quill/Info.plist").appending(queryItems: [.init(name: "ref", value: sha)])
        let bytes = try await read(plist, session: session, accept: "application/vnd.github.raw+json")
        guard let info = try? PropertyListDecoder().decode(AppInfo.self, from: bytes),
              info.CFBundleIdentifier == "com.digimata.quill",
              let version = Version(info.CFBundleShortVersionString, build: info.CFBundleVersion) else {
            throw CheckError.unreadableVersion
        }
        return (info, version)
    }

    private static func isHex(_ value: String, count: Int) -> Bool {
        value.utf8.count == count && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }

    private static func available(installed: Version, published: Version, commit sha: String, restart: Candidate? = nil) -> Result {
        .available(
            installed: installed.label, published: published.label,
            source: repository.appending(path: "archive/\(sha).zip"),
            guide: URL(string: "https://github.com/Abraze101/quill/blob/\(sha)/docs/updating-local-source.md#install-the-update-shown-in-quill")!,
            restart: restart
        )
    }

    private static func read(_ url: URL, session: URLSession,
                             accept: String = "application/vnd.github+json") async throws -> Data {
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 10)
        request.setValue(accept, forHTTPHeaderField: "Accept")
        request.setValue("Quill-Update-Checker", forHTTPHeaderField: "User-Agent")
        do {
            let (bytes, response) = try await session.bytes(for: request, delegate: NoRedirect())
            guard let response = response as? HTTPURLResponse, response.url == url else { throw CheckError.unavailable }
            if response.statusCode == 429 || (response.statusCode == 403 && response.value(forHTTPHeaderField: "X-RateLimit-Remaining") == "0") {
                throw CheckError.rateLimited
            }
            if response.statusCode == 404 { throw CheckError.notFound }
            guard response.statusCode == 200 else { throw CheckError.unavailable }
            let limit = 64 * 1024
            guard response.expectedContentLength <= limit else { throw CheckError.unreadableVersion }
            var data = Data()
            for try await byte in bytes {
                guard data.count < limit else { throw CheckError.unreadableVersion }
                data.append(byte)
            }
            return data
        } catch let error as CheckError {
            throw error
        } catch {
            if Task.isCancelled { throw CancellationError() }
            throw CheckError.unavailable
        }
    }

    private final class NoRedirect: NSObject, URLSessionTaskDelegate {
        func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                        newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
            completionHandler(nil)
        }
    }
}
