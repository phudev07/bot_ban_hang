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
        private const string Endpoint = "https://gateway.dichvuright.ai/v1";
        private const string Marker = "# VietShare Codex Setup";
        private readonly string codexDirectory;
        private readonly string configPath;
        private readonly string authPath;
        private readonly string statePath;
        private readonly TextBox keyInput;
        private readonly ComboBox modelInput;
        private readonly Label statusLabel;
        private readonly Button applyButton;
        private readonly Button removeButton;

        public SetupForm()
        {
            codexDirectory = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                ".codex"
            );
            configPath = Path.Combine(codexDirectory, "config.toml");
            authPath = Path.Combine(codexDirectory, "auth.json");
            statePath = Path.Combine(codexDirectory, ".vietshare-setup-state");

            Text = "VietShare Codex Setup";
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(720, 560);
            MinimumSize = new Size(680, 540);
            BackColor = Color.FromArgb(14, 18, 22);
            ForeColor = Color.FromArgb(235, 240, 242);
            Font = new Font("Segoe UI", 10F, FontStyle.Regular, GraphicsUnit.Point);
            AutoScaleMode = AutoScaleMode.Dpi;

            Panel accent = new Panel();
            accent.BackColor = Color.FromArgb(235, 104, 64);
            accent.Dock = DockStyle.Top;
            accent.Height = 5;
            Controls.Add(accent);

            Label brand = NewLabel("VIETSHARE / CODEX CONNECT", 28, 25, 430, 24);
            brand.Font = new Font("Bahnschrift SemiBold", 10F, FontStyle.Bold);
            brand.ForeColor = Color.FromArgb(235, 104, 64);
            Controls.Add(brand);

            Label title = NewLabel("K\u1ebft n\u1ed1i Codex b\u1eb1ng API key", 28, 54, 630, 42);
            title.Font = new Font("Bahnschrift SemiBold", 24F, FontStyle.Bold);
            Controls.Add(title);

            Label subtitle = NewLabel(
                "Kh\u00f4ng c\u1ea7n c\u00e0i \u0111\u1eb7t. C\u00f4ng c\u1ee5 ch\u1ec9 ghi c\u1ea5u h\u00ecnh v\u00e0o th\u01b0 m\u1ee5c .codex tr\u00ean m\u00e1y n\u00e0y.",
                30,
                101,
                650,
                28
            );
            subtitle.ForeColor = Color.FromArgb(151, 164, 172);
            Controls.Add(subtitle);

            Panel card = new Panel();
            card.Location = new Point(28, 144);
            card.Size = new Size(664, 300);
            card.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            card.BackColor = Color.FromArgb(25, 31, 37);
            card.BorderStyle = BorderStyle.FixedSingle;
            Controls.Add(card);

            Label keyLabel = NewLabel("API KEY", 22, 20, 140, 22);
            keyLabel.Font = new Font("Bahnschrift SemiBold", 9F, FontStyle.Bold);
            keyLabel.ForeColor = Color.FromArgb(174, 187, 194);
            card.Controls.Add(keyLabel);

            keyInput = new TextBox();
            keyInput.Location = new Point(22, 48);
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
            showKey.Location = new Point(542, 49);
            showKey.Size = new Size(100, 28);
            showKey.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            showKey.ForeColor = Color.FromArgb(190, 201, 207);
            showKey.CheckedChanged += delegate { keyInput.UseSystemPasswordChar = !showKey.Checked; };
            card.Controls.Add(showKey);

            Label modelLabel = NewLabel("MODEL", 22, 96, 140, 22);
            modelLabel.Font = new Font("Bahnschrift SemiBold", 9F, FontStyle.Bold);
            modelLabel.ForeColor = Color.FromArgb(174, 187, 194);
            card.Controls.Add(modelLabel);

            modelInput = new ComboBox();
            modelInput.DropDownStyle = ComboBoxStyle.DropDownList;
            modelInput.Location = new Point(22, 124);
            modelInput.Size = new Size(620, 30);
            modelInput.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            modelInput.BackColor = Color.FromArgb(12, 16, 20);
            modelInput.ForeColor = Color.White;
            modelInput.FlatStyle = FlatStyle.Flat;
            modelInput.Font = new Font("Consolas", 11F);
            modelInput.Items.Add("cx/gpt-5.6-sol");
            modelInput.Items.Add("cx/gpt-5.5");
            modelInput.SelectedIndex = 0;
            card.Controls.Add(modelInput);

            Label endpointLabel = NewLabel("ENDPOINT", 22, 172, 140, 22);
            endpointLabel.Font = new Font("Bahnschrift SemiBold", 9F, FontStyle.Bold);
            endpointLabel.ForeColor = Color.FromArgb(174, 187, 194);
            card.Controls.Add(endpointLabel);

            TextBox endpointInput = new TextBox();
            endpointInput.Location = new Point(22, 200);
            endpointInput.Size = new Size(620, 30);
            endpointInput.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            endpointInput.BackColor = Color.FromArgb(19, 24, 29);
            endpointInput.ForeColor = Color.FromArgb(128, 215, 165);
            endpointInput.BorderStyle = BorderStyle.FixedSingle;
            endpointInput.ReadOnly = true;
            endpointInput.Text = Endpoint;
            endpointInput.Font = new Font("Consolas", 10.5F);
            card.Controls.Add(endpointInput);

            statusLabel = NewLabel("", 22, 248, 620, 34);
            statusLabel.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            statusLabel.ForeColor = Color.FromArgb(151, 164, 172);
            card.Controls.Add(statusLabel);

            applyButton = NewButton("\u00c1P D\u1ee4NG C\u1ea4U H\u00ccNH", 28, 466, 230, Color.FromArgb(235, 104, 64));
            applyButton.Click += ApplySettings;
            Controls.Add(applyButton);

            removeButton = NewButton("X\u00d3A / KH\u00d4I PH\u1ee4C", 270, 466, 210, Color.FromArgb(50, 60, 68));
            removeButton.Click += RemoveSettings;
            Controls.Add(removeButton);

            Button folderButton = NewButton("M\u1ede TH\u01af M\u1ee4C .CODEX", 492, 466, 200, Color.FromArgb(38, 47, 54));
            folderButton.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            folderButton.Click += delegate
            {
                Directory.CreateDirectory(codexDirectory);
                Process.Start("explorer.exe", codexDirectory);
            };
            Controls.Add(folderButton);

            Label footnote = NewLabel(
                "Key ch\u1ec9 \u0111\u01b0\u1ee3c l\u01b0u tr\u00ean m\u00e1y. H\u00e3y \u0111\u00f3ng v\u00e0 m\u1edf l\u1ea1i Codex App / CLI sau khi thay \u0111\u1ed5i.",
                30,
                519,
                650,
                24
            );
            footnote.ForeColor = Color.FromArgb(124, 138, 146);
            Controls.Add(footnote);

            AcceptButton = applyButton;
            RefreshStatus();
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
                Directory.CreateDirectory(codexDirectory);
                EnsureBackupState();
                string model = modelInput.SelectedItem.ToString();
                AtomicWrite(configPath, BuildConfig(model));
                AtomicWrite(authPath, BuildAuth(key));
                keyInput.Clear();
                RefreshStatus();
                MessageBox.Show(
                    "\u0110\u00e3 \u00e1p d\u1ee5ng. H\u00e3y \u0111\u00f3ng v\u00e0 m\u1edf l\u1ea1i Codex App / CLI \u0111\u1ec3 d\u00f9ng c\u1ea5u h\u00ecnh m\u1edbi.",
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
            DialogResult choice = MessageBox.Show(
                "C\u00f4ng c\u1ee5 s\u1ebd kh\u00f4i ph\u1ee5c config.toml v\u00e0 auth.json tr\u01b0\u1edbc l\u00fac \u00e1p d\u1ee5ng VietShare. Ti\u1ebfp t\u1ee5c?",
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
                if (!File.Exists(statePath))
                {
                    MessageBox.Show(
                        "Kh\u00f4ng t\u00ecm th\u1ea5y b\u1ea3n sao l\u01b0u do c\u00f4ng c\u1ee5 t\u1ea1o. Kh\u00f4ng c\u00f3 file n\u00e0o b\u1ecb x\u00f3a.",
                        "Kh\u00f4ng c\u00f3 c\u1ea5u h\u00ecnh \u0111\u1ec3 kh\u00f4i ph\u1ee5c",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Information
                    );
                    return;
                }

                Dictionary<string, string> state = ReadState();
                string backupFolder = state["backup_folder"];
                RestoreFile(
                    configPath,
                    Path.Combine(backupFolder, "config.toml"),
                    state["config_existed"] == "1"
                );
                RestoreFile(
                    authPath,
                    Path.Combine(backupFolder, "auth.json"),
                    state["auth_existed"] == "1"
                );
                File.Delete(statePath);
                RefreshStatus();
                MessageBox.Show(
                    "\u0110\u00e3 x\u00f3a c\u1ea5u h\u00ecnh VietShare v\u00e0 kh\u00f4i ph\u1ee5c tr\u1ea1ng th\u00e1i c\u0169. M\u1edf l\u1ea1i Codex \u0111\u1ec3 \u0111\u0103ng nh\u1eadp t\u00e0i kho\u1ea3n b\u00ecnh th\u01b0\u1eddng.",
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

        private void RefreshStatus()
        {
            bool managed = false;
            try
            {
                managed = File.Exists(statePath) && File.Exists(configPath) &&
                    File.ReadAllText(configPath).Contains(Marker);
            }
            catch (IOException)
            {
                managed = false;
            }
            catch (UnauthorizedAccessException)
            {
                managed = false;
            }
            statusLabel.Text = managed
                ? "TR\u1ea0NG TH\u00c1I: \u0110\u00e3 k\u1ebft n\u1ed1i b\u1eb1ng VietShare. C\u00f3 th\u1ec3 \u00e1p d\u1ee5ng l\u1ea1i \u0111\u1ec3 \u0111\u1ed5i model ho\u1eb7c key."
                : "TR\u1ea0NG TH\u00c1I: Ch\u01b0a \u00e1p d\u1ee5ng. C\u1ea5u h\u00ecnh hi\u1ec7n t\u1ea1i s\u1ebd \u0111\u01b0\u1ee3c sao l\u01b0u tr\u01b0\u1edbc khi thay \u0111\u1ed5i.";
            statusLabel.ForeColor = managed
                ? Color.FromArgb(128, 215, 165)
                : Color.FromArgb(151, 164, 172);
            removeButton.Enabled = File.Exists(statePath);
        }

        private void EnsureBackupState()
        {
            if (File.Exists(statePath))
            {
                return;
            }

            string backupFolder = Path.Combine(
                codexDirectory,
                "vietshare-backup",
                DateTime.Now.ToString("yyyyMMdd-HHmmss")
            );
            Directory.CreateDirectory(backupFolder);
            bool configExisted = File.Exists(configPath);
            bool authExisted = File.Exists(authPath);
            if (configExisted)
            {
                File.Copy(configPath, Path.Combine(backupFolder, "config.toml"), true);
            }
            if (authExisted)
            {
                File.Copy(authPath, Path.Combine(backupFolder, "auth.json"), true);
            }

            string state =
                "version=1\r\n" +
                "backup_folder=" + backupFolder + "\r\n" +
                "config_existed=" + (configExisted ? "1" : "0") + "\r\n" +
                "auth_existed=" + (authExisted ? "1" : "0") + "\r\n";
            AtomicWrite(statePath, state);
            File.SetAttributes(statePath, File.GetAttributes(statePath) | FileAttributes.Hidden);
        }

        private Dictionary<string, string> ReadState()
        {
            Dictionary<string, string> values = new Dictionary<string, string>();
            foreach (string line in File.ReadAllLines(statePath))
            {
                int separator = line.IndexOf('=');
                if (separator > 0)
                {
                    values[line.Substring(0, separator)] = line.Substring(separator + 1);
                }
            }
            if (!values.ContainsKey("backup_folder") ||
                !values.ContainsKey("config_existed") ||
                !values.ContainsKey("auth_existed"))
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

        private static string BuildConfig(string model)
        {
            return
                Marker + "\r\n" +
                "model = \"" + model + "\"\r\n" +
                "model_provider = \"vietshare\"\r\n" +
                "cli_auth_credentials_store = \"file\"\r\n\r\n" +
                "[model_providers.vietshare]\r\n" +
                "name = \"VietShare Gateway\"\r\n" +
                "base_url = \"" + Endpoint + "\"\r\n" +
                "wire_api = \"responses\"\r\n" +
                "requires_openai_auth = true\r\n\r\n" +
                "[agents]\r\n" +
                "default_subagent_model = \"" + model + "\"\r\n";
        }

        private static string BuildAuth(string key)
        {
            return
                "{\r\n" +
                "  \"auth_mode\": \"apikey\",\r\n" +
                "  \"OPENAI_API_KEY\": \"" + EscapeJson(key) + "\"\r\n" +
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
