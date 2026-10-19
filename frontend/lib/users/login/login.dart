// lib/users/login.dart
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:frontend/chats/batik_theme.dart';
import 'package:frontend/services/token_storage.dart';
import 'package:go_router/go_router.dart';
import 'package:google_fonts/google_fonts.dart';

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

final _dio = Dio(
  BaseOptions(
    baseUrl: 'http://localhost:8000',
    connectTimeout: const Duration(seconds: 10),
    receiveTimeout: const Duration(seconds: 10),
    headers: {'Content-Type': 'application/json'},
  ),
);

// ---------------------------------------------------------------------------
// Warna & konstanta — dipetakan dari palet batik
// ---------------------------------------------------------------------------

const _bg        = BatikColors.kertas;
const _neon      = BatikColors.marun;    // aksen utama (pengganti neon hijau)
const _neonDim   = Color(0x268B251E);    // marun 15%
const _surface   = BatikColors.putih;
const _border    = BatikColors.marun;
const _textMuted = BatikColors.abuTeks;

// ---------------------------------------------------------------------------
// Intent untuk Shortcut Global
// ---------------------------------------------------------------------------
class SubmitIntent extends Intent {
  const SubmitIntent();
}

// ---------------------------------------------------------------------------
// LoginPage
// ---------------------------------------------------------------------------

class LoginPage extends StatefulWidget {
  const LoginPage({super.key});

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage>
    with SingleTickerProviderStateMixin {
  final _formKey = GlobalKey<FormState>();

  final _identifierController = TextEditingController();
  final _passwordController   = TextEditingController();

  final _identifierFocus = FocusNode();
  final _passwordFocus   = FocusNode();

  bool _obscurePassword = true;
  bool _isLoading       = false;

  late final AnimationController _fadeController;
  late final Animation<double>   _fadeAnimation;

  @override
  void initState() {
    super.initState();
    _fadeController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 900),
    );
    _fadeAnimation = CurvedAnimation(
      parent: _fadeController,
      curve: Curves.easeOut,
    );
    _fadeController.forward();

    // Cek apakah user sudah login sebelumnya
    _checkExistingSession();
  }

  /// Cek session yang sudah ada saat app dimulai
  Future<void> _checkExistingSession() async {
    final accessToken = await TokenStorage.read(key: 'access_token');
    final userId = await TokenStorage.read(key: 'user_id');

    if (accessToken != null && accessToken.isNotEmpty && userId != null) {
      if (mounted) {
        // Langsung arahkan ke halaman chats
        context.go('/chats');
      }
    }
  }

  @override
  void dispose() {
    _fadeController.dispose();
    _identifierController.dispose();
    _passwordController.dispose();
    _identifierFocus.dispose();
    _passwordFocus.dispose();
    super.dispose();
  }

  // ── Hit API login ──────────────────────────────────────────────────────────

  Future<void> _handleLogin() async {
    if (_isLoading) return;
    if (!_formKey.currentState!.validate()) return;

    // Validasi tambahan: bersihkan input
    final identifier = _identifierController.text.trim();
    final password = _passwordController.text;

    if (identifier.isEmpty) {
      _showErrorSnackbar('Username atau email tidak boleh kosong');
      return;
    }

    if (password.isEmpty) {
      _showErrorSnackbar('Password tidak boleh kosong');
      return;
    }

    setState(() => _isLoading = true);

    try {
      final response = await _dio.post(
        '/users/login',
        data: {
          'identifier': identifier,
          'password': password,
        },
      );

      if (response.statusCode == 200 && mounted) {
        final data = response.data['data'] as Map<String, dynamic>;

        final accessToken = data['access_token'] as String;
        final refreshToken = data['refresh_token'] as String;
        final userId = (data['user_id'] as int).toString();

        // Gunakan TokenStorage yang sudah terintegrasi dengan SharedPreferences/FlutterSecureStorage
        await TokenStorage.write(key: 'access_token', value: accessToken);
        await TokenStorage.write(key: 'refresh_token', value: refreshToken);
        await TokenStorage.write(key: 'user_id', value: userId);
        await TokenStorage.write(
          key: 'session_created_at',
          value: DateTime.now().toIso8601String(),
        );

        if (!mounted) return;

        // Tampilkan pesan sukses
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(
              'Login berhasil! Selamat datang kembali.',
              style: GoogleFonts.nunitoSans(
                fontSize: 13,
                color: Colors.white,
              ),
            ),
            backgroundColor: BatikColors.hijau,
            behavior: SnackBarBehavior.floating,
            margin: const EdgeInsets.all(16),
            duration: const Duration(seconds: 2),
          ),
        );

        context.go('/chats');
      }
    } on DioException catch (e) {
      if (!mounted) return;
      String message = 'Terjadi kesalahan. Coba lagi.';

      if (e.type == DioExceptionType.connectionTimeout ||
          e.type == DioExceptionType.receiveTimeout) {
        message = 'Koneksi timeout. Periksa jaringan kamu.';
      } else if (e.response?.statusCode == 401) {
        message = 'Username/email atau password salah.';
      } else if (e.response?.data['detail'] != null) {
        message = e.response!.data['detail'].toString();
      } else if (e.response?.data['message'] != null) {
        message = e.response!.data['message'].toString();
      }

      _showErrorSnackbar(message);
    } catch (e) {
      if (mounted) {
        _showErrorSnackbar('Terjadi kesalahan tidak terduga: ${e.toString()}');
      }
    } finally {
      if (mounted) setState(() => _isLoading = false);
    }
  }

  void _showErrorSnackbar(String message) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          message,
          style: GoogleFonts.nunitoSans(
            fontSize: 13,
            color: BatikColors.sogaGelap,
          ),
        ),
        backgroundColor: BatikColors.putih,
        behavior: SnackBarBehavior.floating,
        margin: const EdgeInsets.all(16),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(10),
          side: const BorderSide(color: BatikColors.marun, width: 1),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: _bg,
      body: SafeArea(
        child: Shortcuts(
          shortcuts: <ShortcutActivator, Intent>{
            const SingleActivator(LogicalKeyboardKey.enter): const SubmitIntent(),
            const SingleActivator(LogicalKeyboardKey.numpadEnter): const SubmitIntent(),
          },
          child: Actions(
            actions: <Type, Action<Intent>>{
              SubmitIntent: CallbackAction<SubmitIntent>(
                onInvoke: (intent) {
                  if (_identifierFocus.hasFocus) {
                    _passwordFocus.requestFocus();
                  } else {
                    _handleLogin();
                  }
                  return null;
                },
              ),
            },
            child: FadeTransition(
              opacity: _fadeAnimation,
              child: SingleChildScrollView(
                padding: const EdgeInsets.symmetric(horizontal: 28, vertical: 40),
                child: Form(
                  key: _formKey,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      _Logo(),
                      const SizedBox(height: 40),
                      Text(
                        'Masuk',
                        style: GoogleFonts.playfairDisplay(
                          fontSize: 28,
                          fontWeight: FontWeight.w700,
                          color: BatikColors.sogaGelap,
                          letterSpacing: -0.5,
                        ),
                      ),
                      const SizedBox(height: 6),
                      Text(
                        'Selamat datang kembali di Ragna.',
                        style: GoogleFonts.nunitoSans(
                          fontSize: 13,
                          color: BatikColors.abuTeks,
                          height: 1.5,
                        ),
                      ),
                      const SizedBox(height: 36),
                      _NeonField(
                        controller: _identifierController,
                        focusNode: _identifierFocus,
                        label: 'Username atau Email',
                        hint: '@johndoe atau johndoe@email.com',
                        icon: Icons.alternate_email_rounded,
                        textInputAction: TextInputAction.next,
                        validator: (v) {
                          if (v == null || v.isEmpty) {
                            return 'Username atau email tidak boleh kosong';
                          }
                          return null;
                        },
                      ),
                      const SizedBox(height: 20),
                      _NeonField(
                        controller: _passwordController,
                        focusNode: _passwordFocus,
                        label: 'Password',
                        hint: '••••••••',
                        icon: Icons.lock_outline_rounded,
                        obscureText: _obscurePassword,
                        textInputAction: TextInputAction.done,
                        suffixIcon: IconButton(
                          icon: Icon(
                            _obscurePassword
                                ? Icons.visibility_off_outlined
                                : Icons.visibility_outlined,
                            color: BatikColors.marun,
                            size: 20,
                          ),
                          onPressed: () =>
                              setState(() => _obscurePassword = !_obscurePassword),
                        ),
                        validator: (v) {
                          if (v == null || v.isEmpty) return 'Password tidak boleh kosong';
                          return null;
                        },
                        onFieldSubmitted: (_) => _handleLogin(),
                      ),
                      const SizedBox(height: 14),
                      Align(
                        alignment: Alignment.centerRight,
                        child: GestureDetector(
                          onTap: () => context.push('/users/reset-password/otp'),
                          child: Text(
                            'Lupa password?',
                            style: GoogleFonts.nunitoSans(
                              fontSize: 12,
                              color: BatikColors.marun,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(height: 32),
                      _NeonButton(
                        label: 'Masuk',
                        isLoading: _isLoading,
                        onPressed: _handleLogin,
                      ),
                      const SizedBox(height: 28),
                      Center(
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Text(
                              'Belum punya akun? ',
                              style: GoogleFonts.nunitoSans(
                                fontSize: 13,
                                color: BatikColors.abuTeks,
                              ),
                            ),
                            GestureDetector(
                              onTap: () => context.push('/users/register'),
                              child: Text(
                                'Daftar sekarang',
                                style: GoogleFonts.nunitoSans(
                                  fontSize: 13,
                                  color: BatikColors.marun,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Logo widget
// ---------------------------------------------------------------------------
class _Logo extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Container(
          width: 48,
          height: 48,
          decoration: BoxDecoration(
            color: BatikColors.marun.withValues(alpha: 0.12),
            borderRadius: BorderRadius.circular(14),
            boxShadow: [
              BoxShadow(
                color: BatikColors.marun.withValues(alpha: 0.2),
                blurRadius: 20,
                spreadRadius: 2,
              ),
            ],
          ),
          child: const Center(
            child: Icon(
              Icons.account_tree_rounded, // Icon graph/tree untuk Graph RAG
              color: BatikColors.marun,
              size: 28,
            ),
          ),
        ),
        const SizedBox(width: 12),
        Text(
          'Ragna',
          style: GoogleFonts.playfairDisplay(
            fontSize: 22,
            fontWeight: FontWeight.w700,
            color: BatikColors.sogaGelap,
            letterSpacing: -0.3,
          ),
        ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// Batik TextField (sebelumnya _NeonField)
// ---------------------------------------------------------------------------

class _NeonField extends StatefulWidget {
  const _NeonField({
    required this.controller,
    required this.label,
    required this.hint,
    required this.icon,
    this.obscureText = false,
    this.suffixIcon,
    this.validator,
    this.textInputAction,
    this.onFieldSubmitted,
    this.focusNode,
  }) : keyboardType = null;

  final TextEditingController controller;
  final String label;
  final String hint;
  final IconData icon;
  final bool obscureText;
  final TextInputType? keyboardType;
  final Widget? suffixIcon;
  final String? Function(String?)? validator;
  final TextInputAction? textInputAction;
  final void Function(String)? onFieldSubmitted;
  final FocusNode? focusNode;

  @override
  State<_NeonField> createState() => _NeonFieldState();
}

class _NeonFieldState extends State<_NeonField> {
  bool _focused = false;
  late FocusNode _effectiveFocusNode;

  @override
  void initState() {
    super.initState();
    _effectiveFocusNode = widget.focusNode ?? FocusNode();
    _effectiveFocusNode.addListener(_handleFocusChange);
  }

  void _handleFocusChange() {
    if (mounted) {
      setState(() {
        _focused = _effectiveFocusNode.hasFocus;
      });
    }
  }

  @override
  void dispose() {
    _effectiveFocusNode.removeListener(_handleFocusChange);
    if (widget.focusNode == null) {
      _effectiveFocusNode.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          widget.label,
          style: GoogleFonts.nunitoSans(
            fontSize: 12,
            fontWeight: FontWeight.w600,
            color: _focused ? BatikColors.marun : BatikColors.abuTeks,
            letterSpacing: 0.3,
          ),
        ),
        const SizedBox(height: 8),
        AnimatedContainer(
          duration: const Duration(milliseconds: 200),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(12),
            boxShadow: _focused
                ? [
                    BoxShadow(
                      color: BatikColors.marun.withValues(alpha: 0.15),
                      blurRadius: 16,
                    ),
                  ]
                : [],
          ),
          child: TextFormField(
            controller: widget.controller,
            focusNode: _effectiveFocusNode,
            obscureText: widget.obscureText,
            keyboardType: widget.keyboardType,
            validator: widget.validator,
            textInputAction: widget.textInputAction,
            onFieldSubmitted: widget.onFieldSubmitted,
            style: GoogleFonts.nunitoSans(
              fontSize: 14,
              color: BatikColors.sogaGelap,
              fontWeight: FontWeight.w500,
            ),
            cursorColor: BatikColors.marun,
            decoration: InputDecoration(
              hintText: widget.hint,
              hintStyle: GoogleFonts.nunitoSans(
                fontSize: 14,
                color: BatikColors.abuTeks,
              ),
              prefixIcon: Icon(
                widget.icon,
                color: _focused ? BatikColors.marun : BatikColors.abuTeks,
                size: 20,
              ),
              suffixIcon: widget.suffixIcon,
              filled: true,
              fillColor: _surface,
              contentPadding: const EdgeInsets.symmetric(
                horizontal: 16,
                vertical: 16,
              ),
              enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(12),
                borderSide: const BorderSide(color: BatikColors.garis, width: 1.5),
              ),
              focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(12),
                borderSide: const BorderSide(color: _border, width: 1.5),
              ),
              errorBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(12),
                borderSide: const BorderSide(color: BatikColors.marun, width: 1.5),
              ),
              focusedErrorBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(12),
                borderSide: const BorderSide(color: BatikColors.marun, width: 1.5),
              ),
              errorStyle: GoogleFonts.nunitoSans(
                fontSize: 11,
                color: BatikColors.marun,
              ),
            ),
          ),
        ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// Batik Button (sebelumnya _NeonButton)
// ---------------------------------------------------------------------------

class _NeonButton extends StatelessWidget {
  const _NeonButton({
    required this.label,
    required this.onPressed,
    this.isLoading = false,
  });

  final String label;
  final VoidCallback onPressed;
  final bool isLoading;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: double.infinity,
      height: 52,
      child: DecoratedBox(
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(14),
          boxShadow: [
            BoxShadow(
              color: BatikColors.marun.withValues(alpha: 0.25),
              blurRadius: 20,
              offset: const Offset(0, 4),
            ),
          ],
        ),
        child: ElevatedButton(
          onPressed: isLoading ? null : onPressed,
          style: ElevatedButton.styleFrom(
            backgroundColor: BatikColors.marun,
            disabledBackgroundColor: BatikColors.marun.withValues(alpha: 0.5),
            foregroundColor: Colors.white,
            elevation: 0,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(14),
            ),
          ),
          child: isLoading
              ? const SizedBox(
                  width: 22,
                  height: 22,
                  child: CircularProgressIndicator(
                    strokeWidth: 2.5,
                    color: Colors.white,
                  ),
                )
              : Text(
                  label,
                  style: GoogleFonts.nunitoSans(
                    fontSize: 15,
                    fontWeight: FontWeight.w700,
                    color: Colors.white,
                    letterSpacing: 0.3,
                  ),
                ),
        ),
      ),
    );
  }
}