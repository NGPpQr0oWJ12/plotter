function Get-XiaoControllerAddress([string]$XiaoPath) {
    try {
        $config = Join-Path (Split-Path $XiaoPath -Parent) 'xparam.dat'
        $bytes = [IO.File]::ReadAllBytes($config)
        if ($bytes.Length -ne 3704 -or [BitConverter]::ToInt32($bytes, 3524) -ne 1234) { return }
        $octets = @(for ($index = 0; $index -lt 4; $index++) {
            [BitConverter]::ToInt32($bytes, 2092 + 4 * $index)
        })
        if ($octets | Where-Object { $_ -lt 0 -or $_ -gt 255 }) { return }
        $address = $octets -join '.'
        if (Test-ControllerAddress $address) { return $address }
    } catch [IO.IOException] {
        return
    } catch [UnauthorizedAccessException] {
        return
    }
}

function Show-MachineSetup($Machine) {
    if ($Machine.width -le 0 -or $Machine.height -le 0) {
        throw 'Не удалось получить размеры рабочего поля из настроек панели.'
    }
    $form = New-Object Windows.Forms.Form
    try {
        $form.Text = 'Подключение станка'
        $form.StartPosition = 'CenterScreen'
        $form.FormBorderStyle = 'FixedDialog'
        $form.MaximizeBox = $false
        $form.MinimizeBox = $false
        $form.ClientSize = [Drawing.Size]::new(525, 258)
        $form.AutoScaleMode = 'Font'

        $instructions = New-Object Windows.Forms.Label
        $instructions.SetBounds(16, 15, 490, 49)
        $instructions.Text = "Рабочее поле панели: $($Machine.width) × $($Machine.height) мм." + [Environment]::NewLine +
            'Сверьте размеры со станком. IP контроллера указан в настройках Xiao.'
        $form.Controls.Add($instructions)

        $ipLabel = New-Object Windows.Forms.Label
        $ipLabel.SetBounds(16, 76, 150, 23)
        $ipLabel.Text = 'IP контроллера'
        $form.Controls.Add($ipLabel)
        $ip = New-Object Windows.Forms.TextBox
        $ip.SetBounds(167, 72, 340, 24)
        $ip.MaxLength = 15
        $form.Controls.Add($ip)

        $xiaoPath = New-Object Windows.Forms.TextBox
        $xiaoPath.SetBounds(16, 109, 345, 24)
        $xiaoPath.ReadOnly = $true
        $form.Controls.Add($xiaoPath)
        $pickXiao = New-Object Windows.Forms.Button
        $pickXiao.SetBounds(369, 106, 138, 29)
        $pickXiao.Text = 'Выбрать Xiao…'
        $form.Controls.Add($pickXiao)
        $pickXiao.Add_Click({
            $dialog = New-Object Windows.Forms.OpenFileDialog
            try {
                $dialog.Title = 'Выберите установленный xiao.exe'
                $dialog.Filter = 'Программа Xiao (xiao.exe)|xiao.exe'
                if ($dialog.ShowDialog($form) -ne 'OK') { return }
                if ([IO.Path]::GetFileName($dialog.FileName) -ine 'xiao.exe') { return }
                $xiaoPath.Text = $dialog.FileName
                $configured = Get-XiaoControllerAddress $xiaoPath.Text
                if (!$ip.Text -and $configured) { $ip.Text = $configured }
            } finally { $dialog.Dispose() }
        })
        $hint = New-Object Windows.Forms.Label
        $hint.SetBounds(16, 140, 491, 33)
        $hint.Text = 'Xiao можно указать, чтобы подставить адрес из его настроек и открывать DXF в Xiao.'
        $form.Controls.Add($hint)

        $confirm = New-Object Windows.Forms.CheckBox
        $confirm.SetBounds(16, 177, 491, 25)
        $confirm.Text = 'Рабочее поле станка совпадает с указанными размерами'
        $form.Controls.Add($confirm)

        $install = New-Object Windows.Forms.Button
        $install.SetBounds(264, 216, 116, 30)
        $install.Text = 'Установить'
        $form.Controls.Add($install)
        $form.AcceptButton = $install
        $install.Add_Click({
            if (!(Test-ControllerAddress $ip.Text.Trim())) {
                [Windows.Forms.MessageBox]::Show($form, 'Введите IPv4-адрес контроллера из настроек станка.',
                    $form.Text, 'OK', 'Warning') | Out-Null
                $ip.Select()
                return
            }
            if (!$confirm.Checked) {
                [Windows.Forms.MessageBox]::Show($form, 'Сверьте размеры со станком и отметьте подтверждение.',
                    $form.Text, 'OK', 'Warning') | Out-Null
                return
            }
            $form.DialogResult = 'OK'
        })

        $cancel = New-Object Windows.Forms.Button
        $cancel.SetBounds(390, 216, 117, 30)
        $cancel.Text = 'Отмена'
        $cancel.DialogResult = 'Cancel'
        $form.CancelButton = $cancel
        $form.Controls.Add($cancel)

        if ($form.ShowDialog() -ne 'OK') { return }
        return @{
            software = @{ path = $xiaoPath.Text }
            observedComputerConfiguration = @{
                machineIp = $ip.Text.Trim()
                workrectMm = @{ x = $Machine.width; y = $Machine.height }
            }
            observedControllerConfiguration = @{ zTravelMm = 0 }
        } | ConvertTo-Json -Depth 5
    } finally { $form.Dispose() }
}
