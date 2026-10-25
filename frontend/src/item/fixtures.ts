import type { Attachment, DiffWithHunks, ItemDetail } from "../api";

// Hunks as the server returns them for backend/tests/fake_jira/files/camera-fix.v2.patch.
export const cameraV2: DiffWithHunks = {
  format: "format-patch",
  files: [
    {
      path: "services/camera/libcameraservice/CameraService.cpp",
      old_path: null,
      status: "modified",
      added: 4,
      removed: 0,
      binary: false,
      hunks: [
        [
          "@@ -1200,6 +1200,10 @@ status_t CameraService::onResume(int cameraId) {",
          "     sp<BasicClient> client = getClient(cameraId);",
          '     ALOGV("%s: camera %d", __FUNCTION__, cameraId);',
          "",
          "+    if (client == nullptr) {",
          '+        ALOGW("%s: no client for camera %d", __FUNCTION__, cameraId);',
          "+        return NO_INIT;",
          "+    }",
          "     client->notifyResume();",
          "     return OK;",
          " }",
        ].join("\n"),
      ],
    },
    {
      path: "services/camera/libcameraservice/CameraService.h",
      old_path: null,
      status: "modified",
      added: 1,
      removed: 1,
      binary: false,
      hunks: [
        [
          "@@ -310,5 +310,5 @@ public:",
          "     virtual status_t    onSuspend(int cameraId);",
          "-    virtual void        onResume(int cameraId);",
          "+    virtual status_t    onResume(int cameraId);",
          " ",
          "     // Debug",
        ].join("\n"),
      ],
    },
  ],
};

export const cameraV1: DiffWithHunks = {
  format: "format-patch",
  files: [{ ...cameraV2.files[0], added: 3, hunks: [cameraV2.files[0].hunks[0].replace("+        return NO_INIT;\n", "")] }],
};

export const icons: DiffWithHunks = {
  format: "plain",
  files: [
    { path: "res/drawable/oem_battery.png", old_path: null, status: "added", added: 0, removed: 0, binary: true, hunks: [] },
  ],
};

export function attachment(jiraId: string, filename: string, overrides: Partial<Attachment> = {}): Attachment {
  return {
    id: Number(jiraId),
    jira_id: jiraId,
    filename,
    size: 2048,
    mime: "text/x-patch",
    kind: "diff",
    jira_url: `https://jira.example.com/secure/attachment/${jiraId}/${filename}`,
    local_url: `/api/workspaces/1/attachments/${jiraId}/${filename}`,
    is_current: true,
    created: "2026-09-25T00:00:00Z",
    parsed: null,
    ...overrides,
  };
}

export const PATCH = "0001-camera-fix-null-deref.patch";

export const cameraAttachments: Attachment[] = [
  attachment("11", PATCH, { is_current: false, created: "2026-09-21T00:00:00Z" }),
  attachment("12", PATCH),
  attachment("13", "icons.patch"),
  attachment("10", "screenshot.png", { kind: "image", mime: "image/png", size: 70 }),
  attachment("14", "logs.zip", { kind: "other", mime: "application/zip", local_url: null, size: 3 * 1024 * 1024 }),
];

export const diffHandlers = {
  "GET /workspaces/1/diffs/11": () => cameraV1,
  "GET /workspaces/1/diffs/12": () => cameraV2,
  "GET /workspaces/1/diffs/13": () => icons,
};

export function itemDetail(overrides: Partial<ItemDetail> = {}): ItemDetail {
  return {
    id: 1,
    local_state: "new",
    dropped: false,
    last_import_error: null,
    created_at: "2026-10-25T19:00:00Z",
    jira_base_url: "https://jira.example.com",
    issue: {
      key: "PROJ-101",
      summary: "Camera HAL crash on resume",
      description_html:
        "<table class='confluenceTable'><tbody><tr><th>Device</th></tr><tr><td>Pixel 8</td></tr></tbody></table><script>window.pwned = true</script>",
      status: "In Progress",
      labels: ["oem-patch", "camera"],
      components: ["Camera"],
      fix_versions: ["2026.Q4"],
      jira_updated: "2026-09-25T16:00:00Z",
      fetched_at: "2026-10-25T19:00:00Z",
      browse_url: "https://jira.example.com/browse/PROJ-101",
      comments: [{ id: 1, author: "Raj Patel", created: "2026-09-22T00:00:00Z", body_html: "<p>Reproduced on <b>AP2A</b>.</p>" }],
      attachments: cameraAttachments,
    },
    cluster: null,
    links: [
      {
        link_type: "Relates",
        direction: "outward",
        description: "relates to",
        item_id: null,
        cluster: null,
        issue: {
          key: "PROJ-201",
          summary: "Audio HAL callback after stream release",
          description_html: "<p>Upstream fix.</p>",
          status: "Closed",
          labels: [],
          components: ["Audio"],
          fix_versions: [],
          jira_updated: "2026-09-18T00:00:00Z",
          fetched_at: "2026-10-25T19:00:00Z",
          browse_url: "https://jira.example.com/browse/PROJ-201",
          comments: [],
          attachments: [],
        },
      },
    ],
    notes: [],
    history: [{ id: 1, type: "imported", payload: { job_id: 1 }, created_at: "2026-10-25T19:00:00Z" }],
    ...overrides,
  };
}
