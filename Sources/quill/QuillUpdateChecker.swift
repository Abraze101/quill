import Foundation

/// Checks the versioned source published on this fork's main branch. Each link
/// refers to the same commit as the Info.plist used for the comparison.
enum QuillUpdateChecker {
    enum Result: Equatable, Sendable {
        case current(version: String)
        case localNewer(installed: String, published: String)
        case available(installed: String, published: String, source: URL, guide: URL)
    }

    enum CheckError: LocalizedError {
        case unknownInstalledVersion, unreadableVersion, rateLimited, unavailable

        var errorDescription: String? {
            switch self {
            case .unknownInstalledVersion: "Quill couldn’t read the version of this installation."
            case .unreadableVersion: "Quill couldn’t verify the version published on GitHub. Try again later."
            case .rateLimited: "GitHub is limiting update checks. Try again later."
            case .unavailable: "Quill couldn’t reach the update information on GitHub. Check your connection and try again."
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
            session: session
        )
    }

    static func check(installedVersion: String?, build: String?, session: URLSession) async throws -> Result {
        guard let installed = Version(installedVersion, build: build) else { throw CheckError.unknownInstalledVersion }
        let branchData = try await read(api.appending(path: "git/ref/heads/main"), session: session)
        guard let branch = try? JSONDecoder().decode(Branch.self, from: branchData),
              branch.ref == "refs/heads/main", branch.object.type == "commit" else { throw CheckError.unreadableVersion }
        let sha = branch.object.sha
        guard sha.utf8.count == 40, sha.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }) else {
            throw CheckError.unreadableVersion
        }
        let plist = api.appending(path: "contents/Sources/quill/Info.plist").appending(queryItems: [.init(name: "ref", value: sha)])
        let plistData = try await read(plist, session: session, accept: "application/vnd.github.raw+json")
        guard let info = try? PropertyListDecoder().decode(AppInfo.self, from: plistData),
              info.CFBundleIdentifier == "com.digimata.quill",
              let published = Version(info.CFBundleShortVersionString, build: info.CFBundleVersion) else {
            throw CheckError.unreadableVersion
        }
        if installed.numbers == published.numbers { return .current(version: installed.label) }
        if published.numbers.lexicographicallyPrecedes(installed.numbers) {
            return .localNewer(installed: installed.label, published: published.label)
        }
        return .available(
            installed: installed.label, published: published.label,
            source: repository.appending(path: "archive/\(sha).zip"),
            guide: URL(string: "https://github.com/Abraze101/quill/blob/\(sha)/docs/updating-local-source.md#install-the-update-shown-in-quill")!
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
