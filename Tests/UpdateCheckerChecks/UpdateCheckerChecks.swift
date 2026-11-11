import Foundation
import os

private struct Reply: Sendable {
    let url: String
    let status: Int
    let data: Data
    let headers: [String: String]

    init(_ url: String, _ data: Data, status: Int = 200, headers: [String: String] = [:]) {
        self.url = url
        self.data = data
        self.status = status
        self.headers = headers
    }
}

private final class MockGitHub: URLProtocol, @unchecked Sendable {
    static let replies = OSAllocatedUnfairLock(initialState: [Reply]())

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}
    override func startLoading() {
        let next = Self.replies.withLock { $0.removeFirst() }
        precondition(request.url?.absoluteString == next.url)
        precondition(request.httpMethod == "GET" && request.httpBody == nil)
        precondition(request.value(forHTTPHeaderField: "Authorization") == nil)
        precondition(request.value(forHTTPHeaderField: "Cookie") == nil)
        let url = request.url!
        let expectedAccept = url.path.hasSuffix(".plist") ? "application/vnd.github.raw+json" : "application/vnd.github+json"
        precondition(request.value(forHTTPHeaderField: "Accept") == expectedAccept)
        let response = HTTPURLResponse(url: url, statusCode: next.status, httpVersion: "HTTP/1.1", headerFields: next.headers)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: next.data)
        client?.urlProtocolDidFinishLoading(self)
    }
}

@main
private enum UpdateCheckerChecks {
    static let sha = String(repeating: "abcd", count: 10)
    static let refURL = "https://api.github.com/repos/Abraze101/quill/git/ref/heads/main"
    static var plistURL: String { "https://api.github.com/repos/Abraze101/quill/contents/Sources/quill/Info.plist?ref=\(sha)" }

    static func branch(_ commit: String = sha) -> Reply {
        Reply(refURL, Data(#"{"ref":"refs/heads/main","object":{"type":"commit","sha":"\#(commit)"}}"#.utf8))
    }

    static func metadata(_ version: String, build: String, bundle: String = "com.digimata.quill") throws -> Reply {
        let data = try PropertyListSerialization.data(fromPropertyList: [
            "CFBundleIdentifier": bundle, "CFBundleShortVersionString": version, "CFBundleVersion": build
        ], format: .xml, options: 0)
        return Reply(plistURL, data)
    }

    static func check(_ replies: [Reply], version: String? = "0.1.9", build: String = "18") async throws -> QuillUpdateChecker.Result {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockGitHub.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        MockGitHub.replies.withLock { $0 = replies }
        defer { precondition(MockGitHub.replies.withLock { $0.isEmpty }) }
        return try await QuillUpdateChecker.check(installedVersion: version, build: build, session: session)
    }

    static func expectFailure(_ replies: [Reply], error: QuillUpdateChecker.CheckError,
                              version: String? = "0.1.9") async throws {
        do {
            _ = try await check(replies, version: version)
            preconditionFailure("Update check unexpectedly succeeded")
        } catch let actual as QuillUpdateChecker.CheckError {
            precondition(actual == error)
        }
    }

    static func main() async throws {
        let equal = try await check([branch(), metadata("0.1.9", build: "18")])
        precondition(equal == .current(version: "0.1.9 (18)"))
        let ahead = try await check([branch(), metadata("0.1.8", build: "99")])
        precondition(ahead == .localNewer(installed: "0.1.9 (18)", published: "0.1.8 (99)"))

        for (version, build) in [("0.1.10", "1"), ("0.1.9", "19"), ("1.0.0", "1")] {
            let result = try await check([branch(), metadata(version, build: build)])
            guard case let .available(installed, published, source, guide) = result else { preconditionFailure() }
            precondition(installed == "0.1.9 (18)" && published == "\(version) (\(build))")
            precondition(source.absoluteString == "https://github.com/Abraze101/quill/archive/\(sha).zip")
            precondition(guide.absoluteString == "https://github.com/Abraze101/quill/blob/\(sha)/docs/updating-local-source.md#install-the-update-shown-in-quill")
        }

        try await expectFailure([], error: .unknownInstalledVersion, version: nil)
        try await expectFailure([branch("main/../../redirect")], error: .unreadableVersion)
        try await expectFailure([branch(), metadata("0.1.20", build: "21", bundle: "org.example.other")], error: .unreadableVersion)
        try await expectFailure([branch(), metadata("0.1.20-beta", build: "21")], error: .unreadableVersion)
        try await expectFailure([branch(), metadata("0.01.9", build: "18")], error: .unreadableVersion)
        try await expectFailure([branch(), metadata("0.1.20", build: "0")], error: .unreadableVersion)
        try await expectFailure([branch(), Reply(plistURL, Data("not a plist".utf8))], error: .unreadableVersion)
        try await expectFailure([Reply(refURL, Data(repeating: 42, count: 65_537))], error: .unreadableVersion)
        try await expectFailure([Reply(refURL, Data(), status: 404)], error: .unavailable)
        try await expectFailure([Reply(refURL, Data(), status: 403, headers: ["X-RateLimit-Remaining": "0"])], error: .rateLimited)
        try await expectFailure([branch(), Reply(plistURL, Data(), status: 429)], error: .rateLimited)
        print("Update checker passed version, build, pinned source links and unavailable-response checks (offline).")
    }
}
