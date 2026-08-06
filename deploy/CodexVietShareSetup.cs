using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Text;
using System.Windows.Forms;

namespace VietShareCodexSetup
{
    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new SetupForm());
        }
    }

    internal sealed class SetupForm : Form
    {
        private const string CodexPlatform = "OpenAI Codex";
        private const string ClaudePlatform = "Claude Code";
        private const string CodexEndpoint = "https://gateway.dichvuright.ai/v1";
        private const string ClaudeEndpoint = "https://gateway.dichvuright.ai";
        private const string CodexMarker = "# VietShare Codex Setup";

        private readonly string codexDirectory;
        private readonly string codexConfigPath;
        private readonly string codexAuthPath;
        private readonly string codexStatePath;
        private readonly string claudeDirectory;
        private readonly string claudeSettingsPath;
        private readonly string claudeStatePath;
        private readonly ComboBox platformInput;
        private readonly TextBox keyInput;
        private readonly ComboBox modelInput;
        private readonly TextBox endpointInput;
        private readonly Label titleLabel;
        private readonly Label subtitleLabel;
        private readonly Label statusLabel;
        private readonly Label footnoteLabel;
        private readonly Button applyButton;
        private readonly Button removeButton;
        private readonly Button folderButton;

        public SetupForm()
        {
            string userProfile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            codexDirectory = Path.Combine(userProfile, ".codex");
            codexConfigPath = Path.Combine(codexDirectory, "config.toml");
            codexAuthPath = Path.Combine(codexDirectory, "auth.json");
            // Keep the old state filename so existing users can still restore their original files.
            codexStatePath = Path.Combine(codexDirectory, ".vietshare-setup-state");
            claudeDirectory = Path.Combine(userProfile, ".claude");
            claudeSettingsPath = Path.Combine(claudeDirectory, "settings.json");
            claudeStatePath = Path.Combine(claudeDirectory, ".vietshare-setup-state");

            Text = "VietShare Codex & Claude Setup";
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(720, 650);
            MinimumSize = new Size(680, 630);
            BackColor = Color.FromArgb(14, 18, 22);
            ForeColor = Color.FromArgb(235, 240, 242);
            Font = new Font("Segoe UI", 10F, FontStyle.Regular, GraphicsUnit.Point);
            AutoScaleMode = AutoScaleMode.Dpi;

            Panel accent = new Panel();
            accent.BackColor = Color.FromArgb(235, 104, 64);
            accent.Dock = DockStyle.Top;
            accent.Height = 5;
            Controls.Add(accent);

            Label brand = NewLabel("VIETSHARE / AI CONNECT", 28, 25, 430, 24);
            brand.Font = new Font("Bahnschrift SemiBold", 10F, FontStyle.Bold);
            brand.ForeColor = Color.FromArgb(235, 104, 64);
            Controls.Add(brand);

            titleLabel = NewLabel("", 28, 54, 650, 42);
            titleLabel.Font = new Font("Bahnschrift SemiBold", 24F, FontStyle.Bold);
            Controls.Add(titleLabel);

            subtitleLabel = NewLabel("", 30, 101, 650, 42);
            subtitleLabel.ForeColor = Color.FromArgb(151, 164, 172);
            Controls.Add(subtitleLabel);

            Panel card = new Panel();
            card.Location = new Point(28, 151);
            card.Size = new Size(664, 378);
            card.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            card.BackColor = Color.FromArgb(25, 31, 37);
            card.BorderStyle = BorderStyle.FixedSingle;
            Controls.Add(card);

            Label platformLabel = NewLabel("NEN TANG", 22, 18, 140, 22);
            platformLabel.Font = new Font("Bahnschrift SemiBold", 9F, FontStyle.Bold);
            platformLabel.ForeColor = Color.FromArgb(174, 187, 194);
            card.Controls.Add(platformLabel);

            platformInput = new ComboBox();
            platformInput.DropDownStyle = ComboBoxStyle.DropDownList;
            platformInput.Location = new Point(22, 44);
            platformInput.Size = new Size(620, 30);
            platformInput.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            platformInput.BackColor = Color.FromArgb(12, 16, 20);
            platformInput.ForeColor = Color.White;
            platformInput.FlatStyle = FlatStyle.Flat;
            platformInput.Font = new Font("Consolas", 11F);
            platformInput.Items.Add(CodexPlatform);
            platformInput.Items.Add(ClaudePlatform);
            platformInput.SelectedIndex = 0;
            platformInput.SelectedIndexChanged += PlatformChanged;
            card.Controls.Add(platformInput);

            Label keyLabel = NewLabel("API KEY", 22, 88, 140, 22);
            keyLabel.Font = new Font("Bahnschrift SemiBold", 9F, FontStyle.Bold);
            keyLabel.ForeColor = Color.FromArgb(174, 187, 194);
            card.Controls.Add(keyLabel);

            keyInput = new TextBox();
            keyInput.Location = new Point(22, 114);
            keyInput.Size = new Size(505, 30);
            keyInput.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            keyInput.BackColor = Color.FromArgb(12, 16, 20);
            keyInput.ForeColor = Color.White;
            keyInput.BorderStyle = BorderStyle.FixedSingle;
            keyInput.UseSystemPasswordChar = true;
            keyInput.Font = new Font("Consolas", 11F);
            card.Controls.Add(keyInput);

            CheckBox showKey = new CheckBox();
            showKey.Text = "Hi\u1ec7n key";
            showKey.Location = new Point(542, 115);
            showKey.Size = new Size(100, 28);
            showKey.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            showKey.ForeColor = Color.FromArgb(190, 201, 207);
            showKey.CheckedChanged += delegate { keyInput.UseSystemPasswordChar = !showKey.Checked; };
            card.Controls.Add(showKey);

            Label modelLabel = NewLabel("MODEL", 22, 158, 140, 22);
            modelLabel.Font = new Font("Bahnschrift SemiBold", 9F, FontStyle.Bold);
            modelLabel.ForeColor = Color.FromArgb(174, 187, 194);
            card.Controls.Add(modelLabel);

            modelInput = new ComboBox();
            modelInput.DropDownStyle = ComboBoxStyle.DropDownList;
            modelInput.Location = new Point(22, 184);
            modelInput.Size = new Size(620, 30);
            modelInput.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            modelInput.BackColor = Color.FromArgb(12, 16, 20);
            modelInput.ForeColor = Color.White;
            modelInput.FlatStyle = FlatStyle.Flat;
            modelInput.Font = new Font("Consolas", 11F);
            card.Controls.Add(modelInput);

            Label endpointLabel = NewLabel("ENDPOINT", 22, 228, 140, 22);
            endpointLabel.Font = new Font("Bahnschrift SemiBold", 9F, FontStyle.Bold);
            endpointLabel.ForeColor = Color.FromArgb(174, 187, 194);
            card.Controls.Add(endpointLabel);

            endpointInput = new TextBox();
            endpointInput.Location = new Point(22, 254);
            endpointInput.Size = new Size(620, 30);
            endpointInput.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            endpointInput.BackColor = Color.FromArgb(19, 24, 29);
            endpointInput.ForeColor = Color.FromArgb(128, 215, 165);
            endpointInput.BorderStyle = BorderStyle.FixedSingle;
            endpointInput.ReadOnly = true;
            endpointInput.Font = new Font("Consolas", 10.5F);
            card.Controls.Add(endpointInput);

            statusLabel = NewLabel("", 22, 302, 620, 54);
            statusLabel.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            statusLabel.ForeColor = Color.FromArgb(151, 164, 172);
            card.Controls.Add(statusLabel);

            applyButton = NewButton("\u00c1P D\u1ee4NG C\u1ea4U H\u00ccNH", 28, 552, 230, Color.FromArgb(235, 104, 64));
            applyButton.Click += ApplySettings;
            Controls.Add(applyButton);

            removeButton = NewButton("X\u00d3A / KH\u00d4I PH\u1ee4C", 270, 552, 210, Color.FromArgb(50, 60, 68));
            removeButton.Click += RemoveSettings;
            Controls.Add(removeButton);

            folderButton = NewButton("", 492, 552, 200, Color.FromArgb(38, 47, 54));
            folderButton.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            folderButton.Click += OpenSelectedFolder;
            Controls.Add(folderButton);

            footnoteLabel = NewLabel("", 30, 605, 650, 34);
            footnoteLabel.ForeColor = Color.FromArgb(124, 138, 146);
            Controls.Add(footnoteLabel);

            AcceptButton = applyButton;
            UpdatePlatformUi();
        }

        private bool IsClaude
        {
            get { return String.Equals(platformInput.SelectedItem as string, ClaudePlatform, StringComparison.Ordinal); }
        }

        private static Label NewLabel(string text, int x, int y, int width, int height)
        {
            Label label = new Label();
            label.Text = text;
            label.Location = new Point(x, y);
            label.Size = new Size(width, height);
            label.AutoEllipsis = true;
            return label;
        }

        private static Button NewButton(string text, int x, int y, int width, Color color)
        {
            Button button = new Button();
            button.Text = text;
            button.Location = new Point(x, y);
            button.Size = new Size(width, 42);
            button.FlatStyle = FlatStyle.Flat;
            button.FlatAppearance.BorderSize = 0;
            button.BackColor = color;
            button.ForeColor = Color.White;
            button.Font = new Font("Bahnschrift SemiBold", 9F, FontStyle.Bold);
            button.Cursor = Cursors.Hand;
            return button;
        }

        private void PlatformChanged(object sender, EventArgs args)
        {
            UpdatePlatformUi();
        }

        private void UpdatePlatformUi()
        {
            modelInput.BeginUpdate();
            modelInput.Items.Clear();
            if (IsClaude)
            {
                modelInput.Items.Add("claude-opus-5");
                modelInput.Items.Add("claude-sonnet-5");
                modelInput.Items.Add("claude-haiku-4-5");
                modelInput.Items.Add("claude-fable-5");
                endpointInput.Text = ClaudeEndpoint;
                titleLabel.Text = "K\u1ebft n\u1ed1i Claude Code b\u1eb1ng API key";
                subtitleLabel.Text = "C\u00f4ng c\u1ee5 ghi settings.json v\u00e0o th\u01b0 m\u1ee5c .claude tr\u00ean m\u00e1y n\u00e0y.";
                folderButton.Text = "M\u1ede TH\u01af M\u1ee4C .CLAUDE";
                footnoteLabel.Text = "H\u00e3y \u0111\u00f3ng v\u00e0 m\u1edf l\u1ea1i Claude Code sau khi thay \u0111\u1ed5i c\u1ea5u h\u00ecnh.";
            }
            else
            {
                modelInput.Items.Add("cx/gpt-5.6-sol");
                modelInput.Items.Add("cx/gpt-5.5");
                endpointInput.Text = CodexEndpoint;
                titleLabel.Text = "K\u1ebft n\u1ed1i Codex b\u1eb1ng API key";
                subtitleLabel.Text = "C\u00f4ng c\u1ee5 ghi config.toml v\u00e0 auth.json v\u00e0o th\u01b0 m\u1ee5c .codex tr\u00ean m\u00e1y n\u00e0y.";
                folderButton.Text = "M\u1ede TH\u01af M\u1ee4C .CODEX";
                footnoteLabel.Text = "H\u00e3y \u0111\u00f3ng v\u00e0 m\u1edf l\u1ea1i Codex App / CLI sau khi thay \u0111\u1ed5i c\u1ea5u h\u00ecnh.";
            }
            modelInput.SelectedIndex = 0;
            modelInput.EndUpdate();
            RefreshStatus();
        }

        private void OpenSelectedFolder(object sender, EventArgs args)
        {
            string directory = IsClaude ? claudeDirectory : codexDirectory;
            Directory.CreateDirectory(directory);
            Process.Start("explorer.exe", directory);
        }

        private void ApplySettings(object sender, EventArgs args)
        {
            string key = keyInput.Text.Trim();
            if (!ValidKey(key))
            {
                MessageBox.Show(
                    "Key ph\u1ea3i b\u1eaft \u0111\u1ea7u b\u1eb1ng sk-, d\u00e0i \u00edt nh\u1ea5t 10 k\u00fd t\u1ef1 v\u00e0 kh\u00f4ng c\u00f3 kho\u1ea3ng tr\u1eafng.",
                    "Key kh\u00f4ng h\u1ee3p l\u1ec7",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Warning
                );
                keyInput.Focus();
                return;
            }

            try
            {
                string model = modelInput.SelectedItem.ToString();
                if (IsClaude)
                {
                    Directory.CreateDirectory(claudeDirectory);
                    EnsureClaudeBackupState();
                    AtomicWrite(claudeSettingsPath, BuildClaudeSettings(model, key));
                }
                else
                {
                    Directory.CreateDirectory(codexDirectory);
                    EnsureCodexBackupState();
                    AtomicWrite(codexConfigPath, BuildCodexConfig(model));
                    AtomicWrite(codexAuthPath, BuildCodexAuth(key));
                }
                keyInput.Clear();
                RefreshStatus();
                string appName = IsClaude ? "Claude Code" : "Codex App / CLI";
                MessageBox.Show(
                    "\u0110\u00e3 \u00e1p d\u1ee5ng. H\u00e3y \u0111\u00f3ng v\u00e0 m\u1edf l\u1ea1i " + appName + " \u0111\u1ec3 d\u00f9ng c\u1ea5u h\u00ecnh m\u1edbi.",
                    "K\u1ebft n\u1ed1i th\u00e0nh c\u00f4ng",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Information
                );
            }
            catch (Exception error)
            {
                MessageBox.Show(
                    "Kh\u00f4ng th\u1ec3 ghi c\u1ea5u h\u00ecnh: " + error.Message,
                    "C\u00f3 l\u1ed7i x\u1ea3y ra",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
            }
        }

        private void RemoveSettings(object sender, EventArgs args)
        {
            string platform = IsClaude ? ClaudePlatform : CodexPlatform;
            DialogResult choice = MessageBox.Show(
                "C\u00f4ng c\u1ee5 s\u1ebd kh\u00f4i ph\u1ee5c c\u1ea5u h\u00ecnh " + platform + " v\u1ec1 tr\u1ea1ng th\u00e1i tr\u01b0\u1edbc l\u00fac \u00e1p d\u1ee5ng VietShare. Ti\u1ebfp t\u1ee5c?",
                "X\u00f3a c\u1ea5u h\u00ecnh VietShare",
                MessageBoxButtons.YesNo,
                MessageBoxIcon.Question
            );
            if (choice != DialogResult.Yes)
            {
                return;
            }

            try
            {
                if (IsClaude)
                {
                    RestoreClaudeSettings();
                }
                else
                {
                    RestoreCodexSettings();
                }
                RefreshStatus();
                MessageBox.Show(
                    "\u0110\u00e3 x\u00f3a c\u1ea5u h\u00ecnh VietShare v\u00e0 kh\u00f4i ph\u1ee5c tr\u1ea1ng th\u00e1i c\u0169 cho " + platform + ".",
                    "\u0110\u00e3 kh\u00f4i ph\u1ee5c",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Information
                );
            }
            catch (Exception error)
            {
                MessageBox.Show(
                    "Kh\u00f4ng th\u1ec3 kh\u00f4i ph\u1ee5c c\u1ea5u h\u00ecnh: " + error.Message,
                    "C\u00f3 l\u1ed7i x\u1ea3y ra",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
            }
        }

        private void RestoreCodexSettings()
        {
            if (!File.Exists(codexStatePath))
            {
                throw new InvalidOperationException("Kh\u00f4ng t\u00ecm th\u1ea5y b\u1ea3n sao l\u01b0u Codex do c\u00f4ng c\u1ee5 t\u1ea1o.");
            }
            Dictionary<string, string> state = ReadState(codexStatePath);
            string backupFolder = state["backup_folder"];
            RestoreFile(codexConfigPath, Path.Combine(backupFolder, "config.toml"), state["config_existed"] == "1");
            RestoreFile(codexAuthPath, Path.Combine(backupFolder, "auth.json"), state["auth_existed"] == "1");
            File.Delete(codexStatePath);
        }

        private void RestoreClaudeSettings()
        {
            if (!File.Exists(claudeStatePath))
            {
                throw new InvalidOperationException("Kh\u00f4ng t\u00ecm th\u1ea5y b\u1ea3n sao l\u01b0u Claude do c\u00f4ng c\u1ee5 t\u1ea1o.");
            }
            Dictionary<string, string> state = ReadState(claudeStatePath);
            string backupFolder = state["backup_folder"];
            RestoreFile(claudeSettingsPath, Path.Combine(backupFolder, "settings.json"), state["settings_existed"] == "1");
            File.Delete(claudeStatePath);
        }

        private void RefreshStatus()
        {
            bool managed = IsClaude ? IsClaudeManaged() : IsCodexManaged();
            string platform = IsClaude ? "Claude Code" : "Codex";
            statusLabel.Text = managed
                ? "TR\u1ea0NG TH\u00c1I: " + platform + " \u0111\u00e3 k\u1ebft n\u1ed1i b\u1eb1ng VietShare. C\u00f3 th\u1ec3 \u00e1p d\u1ee5ng l\u1ea1i \u0111\u1ec3 \u0111\u1ed5i model ho\u1eb7c key."
                : "TR\u1ea0NG TH\u00c1I: Ch\u01b0a \u00e1p d\u1ee5ng cho " + platform + ". C\u1ea5u h\u00ecnh hi\u1ec7n t\u1ea1i s\u1ebd \u0111\u01b0\u1ee3c sao l\u01b0u tr\u01b0\u1edbc khi thay \u0111\u1ed5i.";
            statusLabel.ForeColor = managed
                ? Color.FromArgb(128, 215, 165)
                : Color.FromArgb(151, 164, 172);
            removeButton.Enabled = File.Exists(IsClaude ? claudeStatePath : codexStatePath);
        }

        private bool IsCodexManaged()
        {
            try
            {
                return File.Exists(codexStatePath) && File.Exists(codexConfigPath) &&
                    File.ReadAllText(codexConfigPath).Contains(CodexMarker);
            }
            catch (IOException)
            {
                return false;
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }
        }

        private bool IsClaudeManaged()
        {
            try
            {
                return File.Exists(claudeStatePath) && File.Exists(claudeSettingsPath) &&
                    File.ReadAllText(claudeSettingsPath).Contains(ClaudeEndpoint);
            }
            catch (IOException)
            {
                return false;
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }
        }

        private void EnsureCodexBackupState()
        {
            if (File.Exists(codexStatePath))
            {
                return;
            }
            string backupFolder = NewBackupFolder(codexDirectory);
            bool configExisted = File.Exists(codexConfigPath);
            bool authExisted = File.Exists(codexAuthPath);
            if (configExisted)
            {
                File.Copy(codexConfigPath, Path.Combine(backupFolder, "config.toml"), true);
            }
            if (authExisted)
            {
                File.Copy(codexAuthPath, Path.Combine(backupFolder, "auth.json"), true);
            }
            string state =
                "version=1\r\n" +
                "backup_folder=" + backupFolder + "\r\n" +
                "config_existed=" + (configExisted ? "1" : "0") + "\r\n" +
                "auth_existed=" + (authExisted ? "1" : "0") + "\r\n";
            WriteHiddenState(codexStatePath, state);
        }

        private void EnsureClaudeBackupState()
        {
            if (File.Exists(claudeStatePath))
            {
                return;
            }
            string backupFolder = NewBackupFolder(claudeDirectory);
            bool settingsExisted = File.Exists(claudeSettingsPath);
            if (settingsExisted)
            {
                File.Copy(claudeSettingsPath, Path.Combine(backupFolder, "settings.json"), true);
            }
            string state =
                "version=1\r\n" +
                "backup_folder=" + backupFolder + "\r\n" +
                "settings_existed=" + (settingsExisted ? "1" : "0") + "\r\n";
            WriteHiddenState(claudeStatePath, state);
        }

        private static string NewBackupFolder(string directory)
        {
            string backupFolder = Path.Combine(
                directory,
                "vietshare-backup",
                DateTime.Now.ToString("yyyyMMdd-HHmmss")
            );
            Directory.CreateDirectory(backupFolder);
            return backupFolder;
        }

        private static void WriteHiddenState(string path, string state)
        {
            AtomicWrite(path, state);
            File.SetAttributes(path, File.GetAttributes(path) | FileAttributes.Hidden);
        }

        private static Dictionary<string, string> ReadState(string path)
        {
            Dictionary<string, string> values = new Dictionary<string, string>();
            foreach (string line in File.ReadAllLines(path))
            {
                int separator = line.IndexOf('=');
                if (separator > 0)
                {
                    values[line.Substring(0, separator)] = line.Substring(separator + 1);
                }
            }
            if (!values.ContainsKey("backup_folder"))
            {
                throw new InvalidDataException("File tr\u1ea1ng th\u00e1i sao l\u01b0u kh\u00f4ng h\u1ee3p l\u1ec7.");
            }
            return values;
        }

        private static void RestoreFile(string target, string backup, bool existed)
        {
            if (existed)
            {
                if (!File.Exists(backup))
                {
                    throw new FileNotFoundException("Thi\u1ebfu file sao l\u01b0u", backup);
                }
                AtomicWrite(target, File.ReadAllText(backup, Encoding.UTF8));
            }
            else if (File.Exists(target))
            {
                File.Delete(target);
            }
        }

        private static bool ValidKey(string key)
        {
            if (!key.StartsWith("sk-", StringComparison.Ordinal) || key.Length < 10)
            {
                return false;
            }
            foreach (char character in key)
            {
                if (Char.IsWhiteSpace(character) || Char.IsControl(character) ||
                    character == '"' || character == '\\')
                {
                    return false;
                }
            }
            return true;
        }

        private static string BuildCodexConfig(string model)
        {
            return
                CodexMarker + "\r\n" +
                "model = \"" + model + "\"\r\n" +
                "model_provider = \"vietshare\"\r\n" +
                "cli_auth_credentials_store = \"file\"\r\n\r\n" +
                "[model_providers.vietshare]\r\n" +
                "name = \"VietShare Gateway\"\r\n" +
                "base_url = \"" + CodexEndpoint + "\"\r\n" +
                "wire_api = \"responses\"\r\n" +
                "requires_openai_auth = true\r\n\r\n" +
                "[agents]\r\n" +
                "default_subagent_model = \"" + model + "\"\r\n";
        }

        private static string BuildCodexAuth(string key)
        {
            return
                "{\r\n" +
                "  \"auth_mode\": \"apikey\",\r\n" +
                "  \"OPENAI_API_KEY\": \"" + EscapeJson(key) + "\"\r\n" +
                "}\r\n";
        }

        private static string BuildClaudeSettings(string model, string key)
        {
            return
                "{\r\n" +
                "  \"model\": \"" + EscapeJson(model) + "\",\r\n" +
                "  \"env\": {\r\n" +
                "    \"ANTHROPIC_BASE_URL\": \"" + ClaudeEndpoint + "\",\r\n" +
                "    \"ANTHROPIC_AUTH_TOKEN\": \"" + EscapeJson(key) + "\",\r\n" +
                "    \"ANTHROPIC_MODEL\": \"" + EscapeJson(model) + "\",\r\n" +
                "    \"ANTHROPIC_DEFAULT_OPUS_MODEL\": \"claude-opus-5\",\r\n" +
                "    \"ANTHROPIC_DEFAULT_SONNET_MODEL\": \"claude-sonnet-5\",\r\n" +
                "    \"ANTHROPIC_DEFAULT_HAIKU_MODEL\": \"claude-haiku-4-5\",\r\n" +
                "    \"ANTHROPIC_DEFAULT_FABLE_MODEL\": \"claude-fable-5\"\r\n" +
                "  }\r\n" +
                "}\r\n";
        }

        private static string EscapeJson(string value)
        {
            return value.Replace("\\", "\\\\").Replace("\"", "\\\"");
        }

        private static void AtomicWrite(string path, string content)
        {
            string directory = Path.GetDirectoryName(path);
            Directory.CreateDirectory(directory);
            string temporary = path + ".tmp-" + Guid.NewGuid().ToString("N");
            File.WriteAllText(temporary, content, new UTF8Encoding(false));
            try
            {
                if (File.Exists(path))
                {
                    try
                    {
                        File.Replace(temporary, path, null);
                    }
                    catch (PlatformNotSupportedException)
                    {
                        File.Copy(temporary, path, true);
                        File.Delete(temporary);
                    }
                }
                else
                {
                    File.Move(temporary, path);
                }
            }
            finally
            {
                if (File.Exists(temporary))
                {
                    File.Delete(temporary);
                }
            }
        }
    }
}
