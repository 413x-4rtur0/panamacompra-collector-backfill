package com.panamacompra.monitor.auth

import com.google.firebase.auth.FirebaseAuth
import com.google.firebase.auth.FirebaseUser
import com.google.firebase.auth.GoogleAuthProvider
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.tasks.await

/**
 * Thin wrapper around Firebase Auth: email/password sign-up+sign-in, and
 * Google sign-in via an ID token obtained separately (see MainActivity's
 * Google Sign-In launcher). Auth itself needs internet (Firebase is a cloud
 * service) even though everything else in this app talks to the local LAN
 * monitor — see android/README.md for that hybrid architecture tradeoff.
 */
class AuthRepository {
    private val auth = FirebaseAuth.getInstance()

    val currentUser: FirebaseUser? get() = auth.currentUser

    /** Emits the current user immediately, then again on every sign-in/sign-out. */
    val authState: Flow<FirebaseUser?> = callbackFlow {
        val listener = FirebaseAuth.AuthStateListener { trySend(it.currentUser) }
        auth.addAuthStateListener(listener)
        awaitClose { auth.removeAuthStateListener(listener) }
    }

    suspend fun signUpWithEmail(email: String, password: String): Result<FirebaseUser> = runCatching {
        auth.createUserWithEmailAndPassword(email, password).await().user
            ?: error("Sign-up succeeded but returned no user")
    }

    suspend fun signInWithEmail(email: String, password: String): Result<FirebaseUser> = runCatching {
        auth.signInWithEmailAndPassword(email, password).await().user
            ?: error("Sign-in succeeded but returned no user")
    }

    suspend fun signInWithGoogleIdToken(idToken: String): Result<FirebaseUser> = runCatching {
        val credential = GoogleAuthProvider.getCredential(idToken, null)
        auth.signInWithCredential(credential).await().user
            ?: error("Google sign-in succeeded but returned no user")
    }

    fun signOut() {
        auth.signOut()
    }
}
