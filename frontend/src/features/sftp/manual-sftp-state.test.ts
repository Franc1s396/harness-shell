import { describe, expect, it } from "vitest";

import {
  initialManualSftpState,
  manualSftpReducer,
} from "./manual-sftp-state";

const firstBatch = {
  listing_id: "listing-1",
  path: "/home/user",
  entries: [
    {
      name: "data.txt",
      path: "/home/user/data.txt",
      entry_type: "file" as const,
      size: 4,
      mode: 420,
      mtime_ns: "1",
      link_target: null,
    },
  ],
  next_sequence: 1,
  done: false,
  observed_entry_count: 1,
  complete: false,
};

describe("manual SFTP reducer", () => {
  it("keeps context and directory loading explicit until their requests finish", () => {
    expect(initialManualSftpState.contextLoading).toBe(true);
    expect(initialManualSftpState.listingLoading).toBe(false);

    const contextLoaded = manualSftpReducer(initialManualSftpState, {
      type: "contextLoaded",
      context: {
        ssh_session_id: "ssh-session",
        connection_id: "connection",
        home: "/home/user",
        host_label: "test.example",
        sftp_version: 3,
      },
    });
    expect(contextLoaded.contextLoading).toBe(false);

    const started = manualSftpReducer(contextLoaded, {
      type: "listingStarted",
      path: "/home/user",
    });
    expect(started.listingLoading).toBe(true);

    const partial = manualSftpReducer(started, {
      type: "listingBatch",
      batch: firstBatch,
    });
    expect(partial.listingLoading).toBe(true);

    const completed = manualSftpReducer(partial, {
      type: "listingBatch",
      batch: {
        ...firstBatch,
        entries: [],
        next_sequence: 2,
        done: true,
        observed_entry_count: 1,
        complete: true,
      },
    });
    expect(completed.listingLoading).toBe(false);
  });

  it("does not report an empty recovery list while recovery data is loading", () => {
    const loading = manualSftpReducer(initialManualSftpState, {
      type: "recoveriesLoadStarted",
    });
    expect(loading.recoveriesLoading).toBe(true);

    const loaded = manualSftpReducer(loading, {
      type: "recoveriesLoaded",
      recoveries: [],
    });
    expect(loaded.recoveriesLoading).toBe(false);
  });

  it("accepts ordered listing batches for the started path", () => {
    const started = manualSftpReducer(initialManualSftpState, {
      type: "listingStarted",
      path: "/home/user",
    });
    const listed = manualSftpReducer(started, {
      type: "listingBatch",
      batch: firstBatch,
    });
    const completed = manualSftpReducer(listed, {
      type: "listingBatch",
      batch: {
        ...firstBatch,
        entries: [],
        next_sequence: 2,
        done: true,
        observed_entry_count: 1,
        complete: true,
      },
    });
    expect(completed.listing?.entries).toHaveLength(1);
    expect(completed.listing?.complete).toBe(true);
  });

  it("rejects stale listing ids, paths, and sequences without merging", () => {
    const started = manualSftpReducer(initialManualSftpState, {
      type: "listingStarted",
      path: "/home/user",
    });
    const listed = manualSftpReducer(started, {
      type: "listingBatch",
      batch: firstBatch,
    });
    expect(() =>
      manualSftpReducer(listed, {
        type: "listingBatch",
        batch: { ...firstBatch, listing_id: "stale" },
      }),
    ).toThrow(/listing/i);
    expect(() =>
      manualSftpReducer(listed, {
        type: "listingBatch",
        batch: { ...firstBatch, path: "/other" },
      }),
    ).toThrow(/listing/i);
    expect(() =>
      manualSftpReducer(listed, {
        type: "listingBatch",
        batch: { ...firstBatch, next_sequence: 1 },
      }),
    ).toThrow(/sequence/i);
  });
});
