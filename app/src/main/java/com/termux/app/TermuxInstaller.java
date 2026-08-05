package com.termux.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.ProgressDialog;
import android.content.Context;
import android.os.Build;
import android.os.Environment;
import android.system.Os;
import android.util.Pair;
import android.view.WindowManager;

import com.termux.R;
import com.termux.shared.file.FileUtils;
import com.termux.shared.termux.crash.TermuxCrashUtils;
import com.termux.shared.termux.file.TermuxFileUtils;
import com.termux.shared.interact.MessageDialogUtils;
import com.termux.shared.logger.Logger;
import com.termux.shared.markdown.MarkdownUtils;
import com.termux.shared.errors.Error;
import com.termux.shared.android.PackageUtils;
import com.termux.shared.termux.TermuxConstants;
import com.termux.shared.termux.TermuxUtils;
import com.termux.shared.termux.shell.command.environment.TermuxShellEnvironment;

import java.io.BufferedReader;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.List;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

import static com.termux.shared.termux.TermuxConstants.TERMUX_HOME_DIR;
import static com.termux.shared.termux.TermuxConstants.TERMUX_PREFIX_DIR;
import static com.termux.shared.termux.TermuxConstants.TERMUX_PREFIX_DIR_PATH;
import static com.termux.shared.termux.TermuxConstants.TERMUX_STAGING_PREFIX_DIR;
import static com.termux.shared.termux.TermuxConstants.TERMUX_STAGING_PREFIX_DIR_PATH;

/**
 * Install the Termux bootstrap packages if necessary by following the below steps:
 * <p/>
 * (1) If $PREFIX already exist, assume that it is correct and be done. Note that this relies on that we do not create a
 * broken $PREFIX directory below.
 * <p/>
 * (2) A progress dialog is shown with "Installing..." message and a spinner.
 * <p/>
 * (3) A staging directory, $STAGING_PREFIX, is cleared if left over from broken installation below.
 * <p/>
 * (4) The zip file is loaded from a shared library.
 * <p/>
 * (5) The zip, containing entries relative to the $PREFIX, is is downloaded and extracted by a zip input stream
 * continuously encountering zip file entries:
 * <p/>
 * (5.1) If the zip entry encountered is SYMLINKS.txt, go through it and remember all symlinks to setup.
 * <p/>
 * (5.2) For every other zip entry, extract it into $STAGING_PREFIX and set execute permissions if necessary.
 */
final class TermuxInstaller {

    private static final String LOG_TAG = "TermuxInstaller";

    /** Performs bootstrap setup if necessary. */
    static void setupBootstrapIfNeeded(final Activity activity, final Runnable whenDone) {
        String bootstrapErrorMessage;
        Error filesDirectoryAccessibleError;

        // This will also call Context.getFilesDir(), which should ensure that termux files directory
        // is created if it does not already exist
        filesDirectoryAccessibleError = TermuxFileUtils.isTermuxFilesDirectoryAccessible(activity, true, true);
        boolean isFilesDirectoryAccessible = filesDirectoryAccessibleError == null;

        // Termux can only be run as the primary user (device owner) since only that
        // account has the expected file system paths. Verify that:
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N && !PackageUtils.isCurrentUserThePrimaryUser(activity)) {
            bootstrapErrorMessage = activity.getString(R.string.bootstrap_error_not_primary_user_message,
                MarkdownUtils.getMarkdownCodeForString(TERMUX_PREFIX_DIR_PATH, false));
            Logger.logError(LOG_TAG, "isFilesDirectoryAccessible: " + isFilesDirectoryAccessible);
            Logger.logError(LOG_TAG, bootstrapErrorMessage);
            sendBootstrapCrashReportNotification(activity, bootstrapErrorMessage);
            MessageDialogUtils.exitAppWithErrorMessage(activity,
                activity.getString(R.string.bootstrap_error_title),
                bootstrapErrorMessage);
            return;
        }

        if (!isFilesDirectoryAccessible) {
            bootstrapErrorMessage = Error.getMinimalErrorString(filesDirectoryAccessibleError);
            //noinspection SdCardPath
            if (PackageUtils.isAppInstalledOnExternalStorage(activity) &&
                !TermuxConstants.TERMUX_FILES_DIR_PATH.equals(activity.getFilesDir().getAbsolutePath().replaceAll("^/data/user/0/", "/data/data/"))) {
                bootstrapErrorMessage += "\n\n" + activity.getString(R.string.bootstrap_error_installed_on_portable_sd,
                    MarkdownUtils.getMarkdownCodeForString(TERMUX_PREFIX_DIR_PATH, false));
            }

            Logger.logError(LOG_TAG, bootstrapErrorMessage);
            sendBootstrapCrashReportNotification(activity, bootstrapErrorMessage);
            MessageDialogUtils.showMessage(activity,
                activity.getString(R.string.bootstrap_error_title),
                bootstrapErrorMessage, null);
            return;
        }

        // If prefix directory exists, even if its a symlink to a valid directory and symlink is not broken/dangling
        if (FileUtils.directoryFileExists(TERMUX_PREFIX_DIR_PATH, true)) {
            if (TermuxFileUtils.isTermuxPrefixDirectoryEmpty()) {
                Logger.logInfo(LOG_TAG, "The termux prefix directory \"" + TERMUX_PREFIX_DIR_PATH + "\" exists but is empty or only contains specific unimportant files.");
            } else {
                whenDone.run();
                return;
            }
        } else if (FileUtils.fileExists(TERMUX_PREFIX_DIR_PATH, false)) {
            Logger.logInfo(LOG_TAG, "The termux prefix directory \"" + TERMUX_PREFIX_DIR_PATH + "\" does not exist but another file exists at its destination.");
        }

        final ProgressDialog progress = ProgressDialog.show(activity, null, activity.getString(R.string.bootstrap_installer_body), true, false);
        new Thread() {
            @Override
            public void run() {
                try {
                    Logger.logInfo(LOG_TAG, "Installing " + TermuxConstants.TERMUX_APP_NAME + " bootstrap packages.");

                    Error error;

                    // Delete prefix staging directory or any file at its destination
                    error = FileUtils.deleteFile("termux prefix staging directory", TERMUX_STAGING_PREFIX_DIR_PATH, true);
                    if (error != null) {
                        showBootstrapErrorDialog(activity, whenDone, Error.getErrorMarkdownString(error));
                        return;
                    }

                    // Delete prefix directory or any file at its destination
                    error = FileUtils.deleteFile("termux prefix directory", TERMUX_PREFIX_DIR_PATH, true);
                    if (error != null) {
                        showBootstrapErrorDialog(activity, whenDone, Error.getErrorMarkdownString(error));
                        return;
                    }

                    // Create prefix staging directory if it does not already exist and set required permissions
                    error = TermuxFileUtils.isTermuxPrefixStagingDirectoryAccessible(true, true);
                    if (error != null) {
                        showBootstrapErrorDialog(activity, whenDone, Error.getErrorMarkdownString(error));
                        return;
                    }

                    // Create prefix directory if it does not already exist and set required permissions
                    error = TermuxFileUtils.isTermuxPrefixDirectoryAccessible(true, true);
                    if (error != null) {
                        showBootstrapErrorDialog(activity, whenDone, Error.getErrorMarkdownString(error));
                        return;
                    }

                    Logger.logInfo(LOG_TAG, "Extracting bootstrap zip to prefix staging directory \"" + TERMUX_STAGING_PREFIX_DIR_PATH + "\".");

                    final byte[] buffer = new byte[8096];
                    final List<Pair<String, String>> symlinks = new ArrayList<>(50);

                    final byte[] zipBytes = loadZipBytes();
                    try (ZipInputStream zipInput = new ZipInputStream(new ByteArrayInputStream(zipBytes))) {
                        ZipEntry zipEntry;
                        while ((zipEntry = zipInput.getNextEntry()) != null) {
                            if (zipEntry.getName().equals("SYMLINKS.txt")) {
                                BufferedReader symlinksReader = new BufferedReader(new InputStreamReader(zipInput));
                                String line;
                                while ((line = symlinksReader.readLine()) != null) {
                                    String[] parts = line.split("←");
                                    if (parts.length != 2)
                                        throw new RuntimeException("Malformed symlink line: " + line);
                                    String oldPath = parts[0];
                                    String newPath = TERMUX_STAGING_PREFIX_DIR_PATH + "/" + parts[1];
                                    symlinks.add(Pair.create(oldPath, newPath));

                                    error = ensureDirectoryExists(new File(newPath).getParentFile());
                                    if (error != null) {
                                        showBootstrapErrorDialog(activity, whenDone, Error.getErrorMarkdownString(error));
                                        return;
                                    }
                                }
                            } else {
                                String zipEntryName = zipEntry.getName();
                                File targetFile = new File(TERMUX_STAGING_PREFIX_DIR_PATH, zipEntryName);
                                boolean isDirectory = zipEntry.isDirectory();

                                error = ensureDirectoryExists(isDirectory ? targetFile : targetFile.getParentFile());
                                if (error != null) {
                                    showBootstrapErrorDialog(activity, whenDone, Error.getErrorMarkdownString(error));
                                    return;
                                }

                                if (!isDirectory) {
                                    try (FileOutputStream outStream = new FileOutputStream(targetFile)) {
                                        int readBytes;
                                        while ((readBytes = zipInput.read(buffer)) != -1)
                                            outStream.write(buffer, 0, readBytes);
                                    }
                                    if (zipEntryName.startsWith("bin/") || zipEntryName.startsWith("libexec") ||
                                        zipEntryName.startsWith("lib/apt/apt-helper") || zipEntryName.startsWith("lib/apt/methods")) {
                                        //noinspection OctalInteger
                                        Os.chmod(targetFile.getAbsolutePath(), 0700);
                                    }
                                }
                            }
                        }
                    }

                    if (symlinks.isEmpty())
                        throw new RuntimeException("No SYMLINKS.txt encountered");
                    for (Pair<String, String> symlink : symlinks) {
                        Os.symlink(symlink.first, symlink.second);
                    }

                    patchPrefixInStaging();

                    Logger.logInfo(LOG_TAG, "Moving termux prefix staging to prefix directory.");

                    if (!TERMUX_STAGING_PREFIX_DIR.renameTo(TERMUX_PREFIX_DIR)) {
                        throw new RuntimeException("Moving termux prefix staging to prefix directory failed");
                    }

                    Logger.logInfo(LOG_TAG, "Bootstrap packages installed successfully.");

                    setupFreedomAssets(activity);

                    // Recreate env file since termux prefix was wiped earlier
                    TermuxShellEnvironment.writeEnvironmentToFile(activity);

                    activity.runOnUiThread(whenDone);

                } catch (final Exception e) {
                    showBootstrapErrorDialog(activity, whenDone, Logger.getStackTracesMarkdownString(null, Logger.getStackTracesStringArray(e)));

                } finally {
                    activity.runOnUiThread(() -> {
                        try {
                            progress.dismiss();
                        } catch (RuntimeException e) {
                            // Activity already dismissed - ignore.
                        }
                    });
                }
            }
        }.start();
    }

    public static void showBootstrapErrorDialog(Activity activity, Runnable whenDone, String message) {
        Logger.logErrorExtended(LOG_TAG, "Bootstrap Error:\n" + message);

        // Send a notification with the exception so that the user knows why bootstrap setup failed
        sendBootstrapCrashReportNotification(activity, message);

        activity.runOnUiThread(() -> {
            try {
                new AlertDialog.Builder(activity).setTitle(R.string.bootstrap_error_title).setMessage(R.string.bootstrap_error_body)
                    .setNegativeButton(R.string.bootstrap_error_abort, (dialog, which) -> {
                        dialog.dismiss();
                        activity.finish();
                    })
                    .setPositiveButton(R.string.bootstrap_error_try_again, (dialog, which) -> {
                        dialog.dismiss();
                        FileUtils.deleteFile("termux prefix directory", TERMUX_PREFIX_DIR_PATH, true);
                        TermuxInstaller.setupBootstrapIfNeeded(activity, whenDone);
                    }).show();
            } catch (WindowManager.BadTokenException e1) {
                // Activity already dismissed - ignore.
            }
        });
    }

    private static void sendBootstrapCrashReportNotification(Activity activity, String message) {
        final String title = TermuxConstants.TERMUX_APP_NAME + " Bootstrap Error";

        // Add info of all install Termux plugin apps as well since their target sdk or installation
        // on external/portable sd card can affect Termux app files directory access or exec.
        TermuxCrashUtils.sendCrashReportNotification(activity, LOG_TAG,
            title, null, "## " + title + "\n\n" + message + "\n\n" +
                TermuxUtils.getTermuxDebugMarkdownString(activity),
            true, false, TermuxUtils.AppInfoMode.TERMUX_AND_PLUGIN_PACKAGES, true);
    }

    static void setupStorageSymlinks(final Context context) {
        final String LOG_TAG = "termux-storage";
        final String title = TermuxConstants.TERMUX_APP_NAME + " Setup Storage Error";

        Logger.logInfo(LOG_TAG, "Setting up storage symlinks.");

        new Thread() {
            public void run() {
                try {
                    Error error;
                    File storageDir = TermuxConstants.TERMUX_STORAGE_HOME_DIR;

                    error = FileUtils.clearDirectory("~/storage", storageDir.getAbsolutePath());
                    if (error != null) {
                        Logger.logErrorAndShowToast(context, LOG_TAG, error.getMessage());
                        Logger.logErrorExtended(LOG_TAG, "Setup Storage Error\n" + error.toString());
                        TermuxCrashUtils.sendCrashReportNotification(context, LOG_TAG, title, null,
                            "## " + title + "\n\n" + Error.getErrorMarkdownString(error),
                            true, false, TermuxUtils.AppInfoMode.TERMUX_PACKAGE, true);
                        return;
                    }

                    Logger.logInfo(LOG_TAG, "Setting up storage symlinks at ~/storage/shared, ~/storage/downloads, ~/storage/dcim, ~/storage/pictures, ~/storage/music and ~/storage/movies for directories in \"" + Environment.getExternalStorageDirectory().getAbsolutePath() + "\".");

                    // Get primary storage root "/storage/emulated/0" symlink
                    File sharedDir = Environment.getExternalStorageDirectory();
                    Os.symlink(sharedDir.getAbsolutePath(), new File(storageDir, "shared").getAbsolutePath());

                    File documentsDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOCUMENTS);
                    Os.symlink(documentsDir.getAbsolutePath(), new File(storageDir, "documents").getAbsolutePath());

                    File downloadsDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
                    Os.symlink(downloadsDir.getAbsolutePath(), new File(storageDir, "downloads").getAbsolutePath());

                    File dcimDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DCIM);
                    Os.symlink(dcimDir.getAbsolutePath(), new File(storageDir, "dcim").getAbsolutePath());

                    File picturesDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_PICTURES);
                    Os.symlink(picturesDir.getAbsolutePath(), new File(storageDir, "pictures").getAbsolutePath());

                    File musicDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_MUSIC);
                    Os.symlink(musicDir.getAbsolutePath(), new File(storageDir, "music").getAbsolutePath());

                    File moviesDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_MOVIES);
                    Os.symlink(moviesDir.getAbsolutePath(), new File(storageDir, "movies").getAbsolutePath());

                    File podcastsDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_PODCASTS);
                    Os.symlink(podcastsDir.getAbsolutePath(), new File(storageDir, "podcasts").getAbsolutePath());

                    if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.Q) {
                        File audiobooksDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_AUDIOBOOKS);
                        Os.symlink(audiobooksDir.getAbsolutePath(), new File(storageDir, "audiobooks").getAbsolutePath());
                    }

                    // Dir 0 should ideally be for primary storage
                    // https://cs.android.com/android/platform/superproject/+/android-12.0.0_r32:frameworks/base/core/java/android/app/ContextImpl.java;l=818
                    // https://cs.android.com/android/platform/superproject/+/android-12.0.0_r32:frameworks/base/core/java/android/os/Environment.java;l=219
                    // https://cs.android.com/android/platform/superproject/+/android-12.0.0_r32:frameworks/base/core/java/android/os/Environment.java;l=181
                    // https://cs.android.com/android/platform/superproject/+/android-12.0.0_r32:frameworks/base/services/core/java/com/android/server/StorageManagerService.java;l=3796
                    // https://cs.android.com/android/platform/superproject/+/android-7.0.0_r36:frameworks/base/services/core/java/com/android/server/MountService.java;l=3053

                    // Create "Android/data/com.termux" symlinks
                    File[] dirs = context.getExternalFilesDirs(null);
                    if (dirs != null && dirs.length > 0) {
                        for (int i = 0; i < dirs.length; i++) {
                            File dir = dirs[i];
                            if (dir == null) continue;
                            String symlinkName = "external-" + i;
                            Logger.logInfo(LOG_TAG, "Setting up storage symlinks at ~/storage/" + symlinkName + " for \"" + dir.getAbsolutePath() + "\".");
                            Os.symlink(dir.getAbsolutePath(), new File(storageDir, symlinkName).getAbsolutePath());
                        }
                    }

                    // Create "Android/media/com.termux" symlinks
                    dirs = context.getExternalMediaDirs();
                    if (dirs != null && dirs.length > 0) {
                        for (int i = 0; i < dirs.length; i++) {
                            File dir = dirs[i];
                            if (dir == null) continue;
                            String symlinkName = "media-" + i;
                            Logger.logInfo(LOG_TAG, "Setting up storage symlinks at ~/storage/" + symlinkName + " for \"" + dir.getAbsolutePath() + "\".");
                            Os.symlink(dir.getAbsolutePath(), new File(storageDir, symlinkName).getAbsolutePath());
                        }
                    }

                    Logger.logInfo(LOG_TAG, "Storage symlinks created successfully.");
                } catch (Exception e) {
                    Logger.logErrorAndShowToast(context, LOG_TAG, e.getMessage());
                    Logger.logStackTraceWithMessage(LOG_TAG, "Setup Storage Error: Error setting up link", e);
                    TermuxCrashUtils.sendCrashReportNotification(context, LOG_TAG, title, null,
                        "## " + title + "\n\n" + Logger.getStackTracesMarkdownString(null, Logger.getStackTracesStringArray(e)),
                        true, false, TermuxUtils.AppInfoMode.TERMUX_PACKAGE, true);
                }
            }
        }.start();
    }

    private static Error ensureDirectoryExists(File directory) {
        return FileUtils.createDirectoryFile(directory.getAbsolutePath());
    }

    public static byte[] loadZipBytes() {
        // Only load the shared library when necessary to save memory usage.
        System.loadLibrary("termux-bootstrap");
        return getZip();
    }

    public static native byte[] getZip();

    // Ordered longest-first so shorter replacements don't double-match inside longer paths
    private static final String[][] PREFIX_REPLACEMENTS = {
        {"/data/data/com.termux/files/usr", "/data/data/io.freedom/files/usr"},
        {"/data/data/com.termux/files/home", "/data/data/io.freedom/files/home"},
        {"/data/data/com.termux",            "/data/data/io.freedom"},
    };

    private static void patchPrefixInStaging() {
        Logger.logInfo(LOG_TAG, "Patching prefix paths in staging directory.");
        for (String[] pair : PREFIX_REPLACEMENTS) {
            byte[] oldStr = pair[0].getBytes();
            byte[] newStr = pair[1].getBytes();
            int count = patchAllFiles(TERMUX_STAGING_PREFIX_DIR_PATH, oldStr, newStr);
            Logger.logInfo(LOG_TAG, "  " + pair[0] + " -> " + pair[1] + ": " + count + " files patched.");
        }
    }

    private static int patchAllFiles(String dirPath, byte[] oldStr, byte[] newStr) {
        File dir = new File(dirPath);
        File[] files = dir.listFiles();
        if (files == null) return 0;
        int count = 0;
        for (File f : files) {
            if (f.isDirectory()) {
                count += patchAllFiles(f.getAbsolutePath(), oldStr, newStr);
            } else if (f.isFile()) {
                if (patchFile(f, oldStr, newStr)) count++;
            }
        }
        return count;
    }

    private static boolean patchFile(File file, byte[] oldStr, byte[] newStr) {
        try {
            byte[] data = java.nio.file.Files.readAllBytes(file.toPath());
            int idx = indexOfBytes(data, oldStr);
            if (idx == -1) return false;
            int pos = 0;
            java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream(data.length);
            while (idx != -1) {
                out.write(data, pos, idx - pos);
                out.write(newStr);
                pos = idx + oldStr.length;
                idx = indexOfBytes(data, oldStr, pos);
            }
            out.write(data, pos, data.length - pos);
            java.nio.file.Files.write(file.toPath(), out.toByteArray());
            return true;
        } catch (Exception e) {
            Logger.logError(LOG_TAG, "Failed to patch file " + file.getAbsolutePath() + ": " + e.getMessage());
            return false;
        }
    }

    private static int indexOfBytes(byte[] data, byte[] pattern) {
        return indexOfBytes(data, pattern, 0);
    }

    private static int indexOfBytes(byte[] data, byte[] pattern, int start) {
        for (int i = start; i <= data.length - pattern.length; i++) {
            boolean match = true;
            for (int j = 0; j < pattern.length; j++) {
                if (data[i + j] != pattern[j]) { match = false; break; }
            }
            if (match) return i;
        }
        return -1;
    }

    private static final String FREEDOM_RAW_BASE =
        "https://raw.githubusercontent.com/Eskoxx/Freedom-Android/main/anime_watch/";

    /** Fetch the whole {@code anime_watch} package from GitHub when it is missing.
     *  Only acts as a safety net for existing installs; the first-run download
     *  happens synchronously inside {@link #setupFreedomAssets}. */
    static void updateFreedomAssets() {
        new Thread(() -> {
            try {
                // On a fresh install the prefix is absent and setupFreedomAssets()
                // does the download; skip to avoid double-downloading.
                if (!FileUtils.directoryFileExists(TERMUX_PREFIX_DIR_PATH, true)) return;
                File watchDir = new File(TERMUX_HOME_DIR, "anime_watch");
                if (!new File(watchDir, ".update-version").exists()) {
                    downloadFreedomPackage(watchDir);
                }
            } catch (Exception e) {
                Logger.logError(LOG_TAG, "Failed to download Freedom assets: " + e.getMessage());
            }
        }).start();
    }

    private static byte[] fetchUrl(String url, int timeoutMs) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setInstanceFollowRedirects(true);
        conn.setConnectTimeout(timeoutMs);
        conn.setReadTimeout(timeoutMs);
        conn.setRequestProperty("User-Agent", "Freedom/2.0 (+bootstrap)");
        try (InputStream in = conn.getInputStream();
             ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) != -1) out.write(buf, 0, n);
            return out.toByteArray();
        } finally {
            conn.disconnect();
        }
    }

    private static String sha256Hex(byte[] data) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] d = md.digest(data);
            StringBuilder sb = new StringBuilder();
            for (byte b : d) sb.append(String.format("%02x", b));
            return sb.toString();
        } catch (Exception e) {
            return "";
        }
    }

    private static void writeMarker(File file, String content) throws Exception {
        if (file.getParentFile() != null) file.getParentFile().mkdirs();
        try (FileOutputStream out = new FileOutputStream(file)) {
            out.write(content.getBytes("UTF-8"));
        }
    }

    /** Download the anime_watch package (manifest + all files, sha256-verified)
     *  into {@code $HOME/anime_watch}. */
    private static void downloadFreedomPackage(File watchDir) throws Exception {
        Logger.logInfo(LOG_TAG, "Downloading Freedom anime_watch package from GitHub.");
        watchDir.mkdirs();
        byte[] manifestBytes = fetchUrl(FREEDOM_RAW_BASE + ".update-manifest", 30000);
        String manifest = new String(manifestBytes, "UTF-8");
        for (String line : manifest.split("\n")) {
            line = line.trim();
            if (line.isEmpty() || !line.contains("  ")) continue;
            String[] parts = line.split("  ", 2);
            if (parts.length != 2) continue;
            String digest = parts[0].trim();
            String rel = parts[1].trim();
            int idx = rel.indexOf("anime_watch/");
            if (idx >= 0) rel = rel.substring(idx + "anime_watch/".length());
            byte[] data = fetchUrl(FREEDOM_RAW_BASE + rel, 30000);
            if (!sha256Hex(data).equals(digest)) {
                Logger.logError(LOG_TAG, "Hash mismatch for " + rel + ", skipping");
                continue;
            }
            File dest = new File(watchDir, rel);
            if (dest.getParentFile() != null) dest.getParentFile().mkdirs();
            File tmp = new File(dest.getAbsolutePath() + ".tmp");
            try (FileOutputStream out = new FileOutputStream(tmp)) {
                out.write(data);
            }
            if (!tmp.renameTo(dest)) {
                Logger.logError(LOG_TAG, "Failed to move " + rel);
            }
        }
        writeMarker(new File(watchDir, ".update-manifest"), manifest);
        byte[] versionBytes = fetchUrl(FREEDOM_RAW_BASE + ".update-version", 30000);
        writeMarker(new File(watchDir, ".update-version"), new String(versionBytes, "UTF-8").trim() + "\n");
        Logger.logInfo(LOG_TAG, "Freedom anime_watch package downloaded.");
    }

    static void copyWebtorrentBundle(Context context) {
        new Thread(() -> {
            try {
                File homeDir = TERMUX_HOME_DIR;
                File bundle = new File(homeDir, "webtorrent-bundle.tar");
                if (!bundle.exists()) {
                    try (InputStream in = context.getAssets().open("webtorrent-bundle.tar");
                         OutputStream out = new FileOutputStream(bundle)) {
                        byte[] buf = new byte[4096];
                        int len;
                        while ((len = in.read(buf)) != -1) out.write(buf, 0, len);
                    }
                }
                File prefixLib = new File(TERMUX_PREFIX_DIR, "lib");
                File webtorrentDir = new File(prefixLib, "node_modules/webtorrent-cli");
                File webtorrentBin = new File(TERMUX_PREFIX_DIR_PATH + "/bin", "webtorrent");
                if (!webtorrentBin.exists() || !webtorrentDir.exists()) {
                    if (bundle.exists()) {
                        String bash = TERMUX_PREFIX_DIR_PATH + "/bin/bash";
                        String cmd = "tar xzf " + bundle.getAbsolutePath() +
                            " -C " + prefixLib.getAbsolutePath();
                        new ProcessBuilder(bash, "-c", cmd).start().waitFor();
                        String wrapper =
                            "#!/data/data/io.freedom/files/usr/bin/bash\n" +
                            "exec /data/data/io.freedom/files/usr/bin/node " +
                            "/data/data/io.freedom/files/usr/lib/node_modules/webtorrent-cli/bin/cmd.js \"$@\"\n";
                        try (OutputStream out = new FileOutputStream(webtorrentBin)) {
                            out.write(wrapper.getBytes());
                        }
                        Os.chmod(webtorrentBin.getAbsolutePath(), 0700);
                        bundle.delete();
                    }
                }
                // Patch uint8-util arr2hex/arr2base to accept string inputs
                // (parse-torrent v9+ returns infoHash as string, not Buffer)
                File uint8Util = new File(prefixLib, "node_modules/uint8-util/dist/src/node.js");
                if (uint8Util.exists()) {
                    String content = new String(java.nio.file.Files.readAllBytes(uint8Util.toPath()));
                    String patched = content
                        .replace(
                            "export const arr2base = (data) => Buffer.from(data.buffer, data.byteOffset, data.byteLength).toString('base64');",
                            "export const arr2base = (data) => typeof data === 'string' ? data : Buffer.from(data.buffer, data.byteOffset, data.byteLength).toString('base64');")
                        .replace(
                            "export const arr2hex = (data) => Buffer.from(data.buffer, data.byteOffset, data.byteLength).toString('hex');",
                            "export const arr2hex = (data) => typeof data === 'string' ? data : Buffer.from(data.buffer, data.byteOffset, data.byteLength).toString('hex');");
                    if (!content.equals(patched)) {
                        java.nio.file.Files.write(uint8Util.toPath(), patched.getBytes());
                    }
                }
            } catch (Exception e) {
                Logger.logError(LOG_TAG, "Failed to setup webtorrent bundle: " + e.getMessage());
            }
        }).start();
    }

    private static void setupFreedomAssets(Context context) {
        try {
            Logger.logInfo(LOG_TAG, "Setting up Freedom assets.");

            File homeDir = TERMUX_HOME_DIR;
            if (!homeDir.exists()) homeDir.mkdirs();

            File watchDir = new File(homeDir, "anime_watch");
            downloadFreedomPackage(watchDir);
            File debsDir = new File(homeDir, "debs");
            copyAssetDir(context, "debs", debsDir);

            File setupSh = new File(homeDir, "setup_freedom.sh");
            try (InputStream in = context.getAssets().open("setup_freedom.sh");
                 OutputStream out = new FileOutputStream(setupSh)) {
                byte[] buf = new byte[4096];
                int len;
                while ((len = in.read(buf)) != -1) out.write(buf, 0, len);
            }
            Os.chmod(setupSh.getAbsolutePath(), 0700);

            copyWebtorrentBundle(context);

            File bashrc = new File(homeDir, ".bashrc");
            try (OutputStream out = new FileOutputStream(bashrc)) {
                String bashPath = TERMUX_PREFIX_DIR_PATH + "/bin/bash";
                String content =
                    "SETUP_MARKER=\"$HOME/.freedom_setup_done\"\n" +
                    "if [ ! -f \"$SETUP_MARKER\" ] && [ -x \"$HOME/setup_freedom.sh\" ]; then\n" +
                    "    " + bashPath + " \"$HOME/setup_freedom.sh\"\n" +
                    "    touch \"$SETUP_MARKER\"\n" +
                    "fi\n" +
                    "if [ -d \"$HOME/anime_watch\" ]; then\n" +
                    "    cd \"$HOME\" || return\n" +
                    "    python3 -m anime_watch\n" +
                    "fi\n";
                out.write(content.getBytes());
            }

            Logger.logInfo(LOG_TAG, "Freedom assets setup complete.");
        } catch (Exception e) {
            Logger.logError(LOG_TAG, "Failed to setup Freedom assets: " + e.getMessage());
        }
    }

    private static void copyAssetDir(Context context, String assetPath, File destDir) throws Exception {
        String[] entries;
        try {
            entries = context.getAssets().list(assetPath);
        } catch (Exception e) {
            return;
        }
        if (entries == null || entries.length == 0) return;

        destDir.mkdirs();
        for (String entry : entries) {
            String childAssetPath = assetPath + "/" + entry;
            File childFile = new File(destDir, entry);
            try {
                InputStream in = context.getAssets().open(childAssetPath);
                try (OutputStream out = new FileOutputStream(childFile)) {
                    byte[] buf = new byte[4096];
                    int len;
                    while ((len = in.read(buf)) != -1) out.write(buf, 0, len);
                }
                in.close();
            } catch (java.io.FileNotFoundException e) {
                copyAssetDir(context, childAssetPath, childFile);
            }
        }
    }

}
